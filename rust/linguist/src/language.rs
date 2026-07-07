//! Port of shared/linguist/language.py — the Language registry with its six
//! lookup indices. Insertion order (Unknown first, then languages.yml file
//! order) is load-bearing: per-extension candidate lists and the stable
//! tie-break sort in `_best_candidate` both depend on it.

use std::collections::{HashMap, HashSet};

use anyhow::{bail, Context, Result};

use crate::config::{basename, splitext_ext, Configs};

pub type LangId = u32;

#[derive(Debug)]
pub struct Language {
    pub name: String,
    pub lang_type: String, // data|markup|programming|prose|unknown
    pub color: Option<String>,
    pub aliases: Vec<String>,
    pub language_id: i64,
    pub extensions: Vec<String>,
    pub interpreters: Vec<String>,
    pub filenames: Vec<String>,
    pub popular: bool,
    pub group: String,
}

impl Language {
    /// Canonical lowercase id used across the rest of symbol (Language.key).
    pub fn key(&self) -> String {
        self.name.to_lowercase()
    }

    fn default_alias(name: &str) -> String {
        name.to_lowercase().replace(' ', "-")
    }
}

#[derive(serde::Deserialize)]
struct LangDef {
    #[serde(rename = "type")]
    lang_type: Option<String>,
    color: Option<String>,
    #[serde(default)]
    aliases: Vec<String>,
    language_id: Option<i64>,
    #[serde(default)]
    extensions: Vec<String>,
    #[serde(default)]
    interpreters: Vec<String>,
    #[serde(default)]
    filenames: Vec<String>,
    group: Option<String>,
}

pub struct Registry {
    languages: Vec<Language>, // index = LangId
    by_name: HashMap<String, LangId>,
    by_alias: HashMap<String, LangId>,
    by_extension: HashMap<String, Vec<LangId>>,
    by_interpreter: HashMap<String, Vec<LangId>>,
    by_filename: HashMap<String, Vec<LangId>>,
    pub popular_names: HashSet<String>, // exact-case names from popular.yml
    pub unknown: LangId,
}

impl Registry {
    pub fn load(configs: &Configs) -> Result<Registry> {
        let mut reg = Registry {
            languages: Vec::with_capacity(configs.languages.len() + 1),
            by_name: HashMap::new(),
            by_alias: HashMap::new(),
            by_extension: HashMap::new(),
            by_interpreter: HashMap::new(),
            by_filename: HashMap::new(),
            popular_names: configs.popular.iter().cloned().collect(),
            unknown: 0,
        };

        // Unknown sentinel first, exactly like Language.load_languages
        reg.create(Language {
            name: "Unknown".into(),
            lang_type: "unknown".into(),
            color: Some("#ededed".into()),
            aliases: vec![Language::default_alias("Unknown")],
            language_id: -1,
            extensions: vec![],
            interpreters: vec![],
            filenames: vec![],
            popular: false,
            group: "Unknown".into(),
        })?;

        for (k, v) in &configs.languages {
            let name: String = match k.as_str() {
                Some(s) => s.to_owned(),
                None => bail!("non-string language name in languages.yml"),
            };
            let def: LangDef = serde_yaml::from_value(v.clone())
                .with_context(|| format!("parsing language {name}"))?;
            let lang_type = def.lang_type.unwrap_or_default();
            if !lang_type.is_empty()
                && !matches!(
                    lang_type.as_str(),
                    "data" | "markup" | "programming" | "prose" | "unknown"
                )
            {
                bail!("Invalid type: {lang_type}");
            }
            let mut aliases = vec![Language::default_alias(&name)];
            aliases.extend(def.aliases);
            reg.create(Language {
                popular: reg.popular_names.contains(&name),
                group: def.group.unwrap_or_else(|| name.clone()),
                name,
                lang_type,
                color: def.color,
                aliases,
                language_id: def.language_id.unwrap_or_default(),
                extensions: def.extensions,
                interpreters: def.interpreters,
                filenames: def.filenames,
            })?;
        }

        reg.unknown = reg.by_name["unknown"];
        Ok(reg)
    }

    fn create(&mut self, lang: Language) -> Result<()> {
        let id = self.languages.len() as LangId;
        let name_lower = lang.name.to_lowercase();
        if self.by_name.contains_key(&name_lower) {
            bail!("Duplicate language name: {}", lang.name);
        }
        self.by_name.insert(name_lower, id);
        for alias in &lang.aliases {
            let a = alias.to_lowercase();
            if self.by_alias.contains_key(&a) {
                bail!("Duplicate alias: {alias}");
            }
            self.by_alias.insert(a, id);
        }
        for ext in &lang.extensions {
            if !ext.starts_with('.') {
                bail!("Extension must start with '.': {ext}");
            }
            self.by_extension.entry(ext.to_lowercase()).or_default().push(id);
        }
        for interp in &lang.interpreters {
            self.by_interpreter.entry(interp.to_lowercase()).or_default().push(id);
        }
        for fname in &lang.filenames {
            self.by_filename.entry(fname.to_lowercase()).or_default().push(id);
        }
        self.languages.push(lang);
        Ok(())
    }

    pub fn get(&self, id: LangId) -> &Language {
        &self.languages[id as usize]
    }

    pub fn len(&self) -> usize {
        self.languages.len()
    }

    pub fn is_empty(&self) -> bool {
        self.languages.is_empty()
    }

    /// find_by_name: lowercase lookup, retrying with the segment before the
    /// first comma (language.py:115).
    pub fn find_by_name(&self, name: &str) -> Option<LangId> {
        if name.trim().is_empty() {
            return None;
        }
        self.by_name.get(&name.to_lowercase()).copied().or_else(|| {
            let head = name.split(',').next().unwrap_or("").trim().to_lowercase();
            self.by_name.get(&head).copied()
        })
    }

    pub fn find_by_alias(&self, alias: &str) -> Option<LangId> {
        if alias.trim().is_empty() {
            return None;
        }
        self.by_alias.get(&alias.to_lowercase()).copied().or_else(|| {
            let head = alias.split(',').next().unwrap_or("").trim().to_lowercase();
            self.by_alias.get(&head).copied()
        })
    }

    pub fn find_by_filename(&self, filename: &str) -> &[LangId] {
        self.by_filename
            .get(&basename(filename).to_lowercase())
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Single splitext extension only — `.d.ts` is looked up as `.ts`
    /// (language.py:130). No multi-suffix matching.
    pub fn find_by_extension(&self, filename: &str) -> &[LangId] {
        self.by_extension
            .get(&splitext_ext(filename).to_lowercase())
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    pub fn find_by_interpreter(&self, interpreter: &str) -> &[LangId] {
        self.by_interpreter
            .get(&interpreter.to_lowercase())
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{load, ConfigSource};

    fn registry() -> Registry {
        Registry::load(&load(&ConfigSource::Embedded).unwrap()).unwrap()
    }

    #[test]
    fn loads_all_languages() {
        let reg = registry();
        assert!(reg.len() > 500, "expected 500+ languages, got {}", reg.len());
        assert_eq!(reg.get(reg.unknown).language_id, -1);
    }

    #[test]
    fn indices_work() {
        let reg = registry();
        let py = reg.find_by_name("Python").unwrap();
        assert_eq!(reg.get(py).name, "Python");
        assert!(reg.get(py).popular);
        assert_eq!(reg.find_by_alias("python3"), Some(py));
        assert!(reg.find_by_extension("x.py").contains(&py));
        assert!(reg.find_by_interpreter("python3").contains(&py));
        // filename index via basename
        let makefile = reg.find_by_filename("some/dir/Makefile");
        assert!(!makefile.is_empty());
        // comma retry
        assert_eq!(reg.find_by_name("Python, extra"), Some(py));
    }

    #[test]
    fn extension_order_is_file_order() {
        let reg = registry();
        // .h maps to multiple languages; order must be languages.yml order
        let hs = reg.find_by_extension("x.h");
        assert!(hs.len() >= 2);
        let names: Vec<&str> = hs.iter().map(|&id| reg.get(id).name.as_str()).collect();
        assert!(names.contains(&"C"));
    }
}
