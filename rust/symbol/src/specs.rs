//! Port of the spec system's file-scope layers (shared/spec.py,
//! shared/project_config.py, shared/context.py): global baseline →
//! per-package specs for the project's declared dependencies → project
//! config. Dependency discovery itself belongs to the language adapters.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

include!(concat!(env!("OUT_DIR"), "/embedded_specs.rs"));

/// A spec file relative to data/: from `$SYMBOL_REPO` when set (dev
/// checkout), otherwise the copy embedded at build time.
fn data_file(rel: &str) -> Option<String> {
    if let Ok(repo) = std::env::var("SYMBOL_REPO") {
        let path = Path::new(&repo).join("src/wyolet/symbol/data").join(rel);
        return std::fs::read_to_string(path).ok();
    }
    SPEC_FILES
        .iter()
        .find(|(name, _)| *name == rel)
        .map(|(_, text)| (*text).to_owned())
}

fn parse(text: &str) -> Option<toml::Table> {
    text.parse::<toml::Table>().ok()
}

fn strings_at(table: &toml::Table, section: &str, key: &str) -> Vec<String> {
    table
        .get(section)
        .and_then(|s| s.get(key))
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

/// The `[tool.symbol]` / symbol.toml fields that scope file collection.
#[derive(Default, Debug)]
pub struct ProjectConfig {
    pub checker_include: Vec<String>,
    pub checker_exclude: Vec<String>,
    pub scanner_exclude: Vec<String>,
    pub extra_specs: Vec<String>,
}

/// load_project_config: a non-empty, parseable symbol.toml wins outright;
/// otherwise `[tool.symbol]` in pyproject.toml.
pub fn load_project_config(project_root: &Path) -> ProjectConfig {
    let standalone = std::fs::read_to_string(project_root.join("symbol.toml"))
        .ok()
        .and_then(|t| parse(&t))
        .filter(|t| !t.is_empty());
    let conf = standalone.or_else(|| {
        let pyproject = parse(&std::fs::read_to_string(project_root.join("pyproject.toml")).ok()?)?;
        pyproject
            .get("tool")?
            .get("symbol")?
            .as_table()
            .cloned()
            .filter(|t| !t.is_empty())
    });
    let Some(conf) = conf else {
        return ProjectConfig::default();
    };
    ProjectConfig {
        checker_include: strings_at(&conf, "checker", "include"),
        checker_exclude: strings_at(&conf, "checker", "exclude"),
        scanner_exclude: strings_at(&conf, "scanner", "exclude"),
        extra_specs: conf
            .get("extra_specs")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

/// PEP 503-style key the spec catalog is indexed by.
pub fn normalize_package_name(name: &str) -> String {
    name.to_lowercase().replace(['_', '.'], "-")
}

/// load_spec(project_deps=deps): every `[specs] include` entry whose name is
/// a declared dependency, every stdlib spec, then the project's extra_specs
/// (overriding a built-in of the same name). Returns (name, spec) pairs.
fn active_package_specs(
    project_root: &Path,
    config: &ProjectConfig,
    deps: &[String],
) -> Vec<(String, toml::Table)> {
    let deps: HashSet<String> = deps.iter().map(|d| normalize_package_name(d)).collect();
    let global = data_file("spec.toml")
        .and_then(|t| parse(&t))
        .unwrap_or_default();
    let mut active: Vec<(String, toml::Table)> = vec![];
    let mut put = |spec: toml::Table| {
        let Some(name) = spec.get("name").and_then(|n| n.as_str()).map(str::to_owned) else {
            return;
        };
        match active.iter_mut().find(|(n, _)| *n == name) {
            Some(slot) => slot.1 = spec,
            None => active.push((name, spec)),
        }
    };
    for pkg in strings_at(&global, "specs", "include") {
        let Some(spec) = data_file(&format!("specs/{pkg}/spec.toml")).and_then(|t| parse(&t))
        else {
            continue;
        };
        let stdlib = spec.get("stdlib").and_then(|v| v.as_bool()) == Some(true);
        if deps.contains(&normalize_package_name(&pkg)) || stdlib {
            put(spec);
        }
    }
    for extra in &config.extra_specs {
        let path = PathBuf::from(extra);
        let path = if path.is_absolute() {
            path
        } else {
            project_root.join(path)
        };
        if let Some(spec) = std::fs::read_to_string(path).ok().and_then(|t| parse(&t)) {
            put(spec);
        }
    }
    active
}

/// build_context's checker scope: (include, exclude). Exclude is the global
/// `[checker] exclude` + active package specs' + the project's.
pub fn checker_scope(project_root: &Path, deps: &[String]) -> (Vec<String>, Vec<String>) {
    let config = load_project_config(project_root);
    let global = data_file("spec.toml")
        .and_then(|t| parse(&t))
        .unwrap_or_default();
    let mut exclude = strings_at(&global, "checker", "exclude");
    for (_, spec) in active_package_specs(project_root, &config, deps) {
        exclude.extend(strings_at(&spec, "checker", "exclude"));
    }
    exclude.extend(config.checker_exclude.iter().cloned());
    let include = if config.checker_include.is_empty() {
        strings_at(&global, "checker", "include")
    } else {
        config.checker_include
    };
    (include, exclude)
}

/// The exclude set every build applies before dependencies are known:
/// global baseline + project config.
pub fn base_checker_excludes(project_root: &Path) -> Vec<String> {
    checker_scope(project_root, &[]).1
}

/// Global `[scanner] exclude` list merged with the project's.
pub fn scanner_excludes(project_root: &Path) -> Vec<String> {
    let global = data_file("spec.toml")
        .and_then(|t| parse(&t))
        .unwrap_or_default();
    let mut out = strings_at(&global, "scanner", "exclude");
    out.extend(load_project_config(project_root).scanner_exclude);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dependency_activates_package_excludes() {
        let dir = tempfile::tempdir().unwrap();
        let (_, without) = checker_scope(dir.path(), &[]);
        assert!(!without.iter().any(|p| p == "docs_src/**"));
        let (_, with) = checker_scope(dir.path(), &["FastAPI".to_owned()]);
        assert!(with.iter().any(|p| p == "docs_src/**"), "{with:?}");
        assert!(with.iter().any(|p| p == "**/.venv/**"));
    }

    #[test]
    fn symbol_toml_wins_over_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[tool.symbol.checker]\nexclude = [\"from_pyproject/**\"]\n",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("symbol.toml"),
            "[checker]\nexclude = [\"from_symbol_toml/**\"]\n",
        )
        .unwrap();
        let (_, exclude) = checker_scope(dir.path(), &[]);
        assert!(exclude.iter().any(|p| p == "from_symbol_toml/**"));
        assert!(!exclude.iter().any(|p| p == "from_pyproject/**"));
    }

    #[test]
    fn extra_spec_contributes_excludes() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[tool.symbol]\nextra_specs = [\"local.toml\"]\n",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("local.toml"),
            "name = \"local\"\ncategory = \"web\"\n[checker]\nexclude = [\"gen/**\"]\n",
        )
        .unwrap();
        let (_, exclude) = checker_scope(dir.path(), &[]);
        assert!(exclude.iter().any(|p| p == "gen/**"));
    }

    #[test]
    fn catalog_is_embedded() {
        assert!(SPEC_FILES
            .iter()
            .any(|(n, _)| *n == "specs/fastapi/spec.toml"));
        assert_eq!(normalize_package_name("Foo_Bar.baz"), "foo-bar-baz");
    }
}
