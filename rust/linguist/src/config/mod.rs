//! Port of shared/linguist/config/load.py. Config YAMLs resolve from
//! `--config-dir`, then `SYMBOL_LINGUIST_CONFIG_DIR`, then the embedded
//! copies baked in at build time — so `symbol update-linguist` (which
//! overwrites the Python package's config dir) keeps working when the
//! binary is pointed at that dir.

pub mod embedded;

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

pub const CONFIG_FILES: [&str; 6] = [
    "documentation.yml",
    "vendor.yml",
    "popular.yml",
    "languages.yml",
    "heuristics.yml",
    "generic.yml",
];

pub enum ConfigSource {
    Dir(PathBuf),
    Embedded,
}

impl ConfigSource {
    pub fn resolve(cli_dir: Option<PathBuf>) -> ConfigSource {
        if let Some(d) = cli_dir {
            return ConfigSource::Dir(d);
        }
        if let Ok(d) = std::env::var("SYMBOL_LINGUIST_CONFIG_DIR") {
            if !d.is_empty() {
                return ConfigSource::Dir(PathBuf::from(d));
            }
        }
        ConfigSource::Embedded
    }

    fn read(&self, name: &str) -> Result<String> {
        match self {
            ConfigSource::Dir(dir) => std::fs::read_to_string(dir.join(name))
                .with_context(|| format!("reading {name} from {}", dir.display())),
            ConfigSource::Embedded => Ok(embedded::get(name).to_owned()),
        }
    }

    /// Fingerprint of the config source for cache invalidation: (name, size,
    /// mtime_ns) per file for a dir source; a build-time constant for the
    /// embedded copies.
    pub fn fingerprint(&self) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        match self {
            ConfigSource::Dir(dir) => {
                for name in CONFIG_FILES {
                    name.hash(&mut h);
                    if let Ok(md) = std::fs::metadata(dir.join(name)) {
                        md.len().hash(&mut h);
                        if let Ok(m) = md.modified() {
                            if let Ok(d) = m.duration_since(std::time::UNIX_EPOCH) {
                                d.as_nanos().hash(&mut h);
                            }
                        }
                    }
                }
            }
            ConfigSource::Embedded => {
                "embedded".hash(&mut h);
                env!("CARGO_PKG_VERSION").hash(&mut h);
            }
        }
        h.finish()
    }
}

pub struct Configs {
    /// languages.yml as an ORDER-PRESERVING mapping — registry insertion
    /// order and per-extension candidate order both depend on file order.
    pub languages: serde_yaml::Mapping,
    pub popular: Vec<String>,
    pub generic_extensions: Vec<String>,
    pub vendor_patterns: Vec<String>,
    pub heuristics: serde_yaml::Value,
    pub fingerprint: u64,
}

pub fn load(source: &ConfigSource) -> Result<Configs> {
    let languages: serde_yaml::Mapping =
        serde_yaml::from_str(&source.read("languages.yml")?).context("parsing languages.yml")?;
    let popular: Vec<String> =
        serde_yaml::from_str(&source.read("popular.yml")?).context("parsing popular.yml")?;
    #[derive(serde::Deserialize)]
    struct Generic {
        #[serde(default)]
        extensions: Vec<String>,
    }
    let generic: Generic =
        serde_yaml::from_str(&source.read("generic.yml")?).context("parsing generic.yml")?;
    let vendor_patterns: Vec<String> =
        serde_yaml::from_str(&source.read("vendor.yml")?).context("parsing vendor.yml")?;
    let heuristics: serde_yaml::Value =
        serde_yaml::from_str(&source.read("heuristics.yml")?).context("parsing heuristics.yml")?;
    Ok(Configs {
        languages,
        popular,
        generic_extensions: generic.extensions,
        vendor_patterns,
        heuristics,
        fingerprint: source.fingerprint(),
    })
}

/// Port of load_vendor_regex: fold vendor.yml into ONE alternation, skipping
/// patterns that don't compile (Ruby-isms). Returns (regex, dropped_count);
/// `None` regex = nothing compiled = never matches.
pub fn build_vendor_regex(patterns: &[String]) -> (Option<regex::Regex>, usize) {
    let mut valid: Vec<String> = Vec::with_capacity(patterns.len());
    let mut dropped = 0usize;
    for p in patterns {
        // Python uses the `re` engine here; our fast engine is the analogue.
        // fancy-regex is NOT tried: one giant alternation with backtracking
        // would be a per-path cost, and Python drops these patterns too.
        if regex::Regex::new(p).is_ok() {
            valid.push(format!("(?:{p})"));
        } else {
            dropped += 1;
        }
    }
    if valid.is_empty() {
        return (None, dropped);
    }
    match regex::Regex::new(&valid.join("|")) {
        Ok(r) => (Some(r), dropped),
        Err(_) => (None, patterns.len()),
    }
}

/// `os.path.splitext(name)[1]` for a basename: the last `.`-suffix, unless
/// the stem before it is all dots (`.bashrc`, `..b` → no extension).
pub fn splitext_ext(name: &str) -> &str {
    match name.rfind('.') {
        Some(dot) if name[..dot].chars().any(|c| c != '.') => &name[dot..],
        _ => "",
    }
}

/// Basename of a forward-slash or OS path string (os.path.basename).
pub fn basename(path: &str) -> &str {
    path.rsplit(['/', std::path::MAIN_SEPARATOR]).next().unwrap_or(path)
}

pub fn python_config_dir(repo_root: &Path) -> PathBuf {
    repo_root.join("src/wyolet/symbol/shared/linguist/config")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitext_matches_cpython() {
        assert_eq!(splitext_ext("a.py"), ".py");
        assert_eq!(splitext_ext("a.tar.gz"), ".gz");
        assert_eq!(splitext_ext(".bashrc"), "");
        assert_eq!(splitext_ext("..bashrc"), "");
        assert_eq!(splitext_ext("..b.c"), ".c");
        assert_eq!(splitext_ext("a..b"), ".b");
        assert_eq!(splitext_ext("noext"), "");
        assert_eq!(splitext_ext("..."), "");
        assert_eq!(splitext_ext("A.PY"), ".PY");
    }

    #[test]
    fn vendor_regex_compiles_most_patterns() {
        let cfg = load(&ConfigSource::Embedded).unwrap();
        let (re, dropped) = build_vendor_regex(&cfg.vendor_patterns);
        let re = re.expect("some vendor patterns must compile");
        // sanity: the classics match
        assert!(re.is_match("node_modules/"));
        assert!(re.is_match("a/node_modules/"));
        assert!(re.is_match("vendor/"));
        assert!(!re.is_match("src/main.py"));
        // Ruby-isms may drop, but the bulk must survive
        assert!(dropped < 20, "dropped {dropped} vendor patterns");
    }
}
