//! Language adapter registry — Rust side of adapters/registry.py, with every
//! adapter an out-of-process native-AST daemon (pyscan / go-scan) behind the
//! symbol.rpc wire contract.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use anyhow::{bail, Result};
use serde::Deserialize;

use crate::rpc::Daemon;

// ---------------------------------------------------------- wire types
// Mirror protocols/types.py (ScannedSymbol / ScannedImport / FileScan).

#[derive(Deserialize, Debug, Clone)]
pub struct ScannedRef {
    pub name: String,
    pub kind: String, // "name" | "attr"
    pub line: i64,
}

#[derive(Deserialize, Debug, Clone)]
pub struct ScannedSymbol {
    pub kind: String,
    pub name: String,
    pub qualified_path: String,
    pub byte_range: (i64, i64),
    pub line_range: (i64, i64),
    #[serde(default)]
    pub refs: Vec<ScannedRef>,
    #[serde(default)]
    pub bases: Vec<String>,
    #[serde(default)]
    pub children: Vec<ScannedSymbol>,
}

#[derive(Deserialize, Debug, Clone)]
pub struct ScannedImport {
    pub local: String,
    pub source: String,
    pub line: i64,
}

#[derive(Deserialize, Debug, Clone)]
pub struct FileScan {
    pub language: String,
    #[serde(default)]
    pub imports: Vec<ScannedImport>,
    #[serde(default)]
    pub symbols: Vec<ScannedSymbol>,
    #[serde(default = "default_true")]
    pub ok: bool,
    #[serde(default)]
    pub error: Option<String>,
}

fn default_true() -> bool {
    true
}

// Mirror protocols/types.py rename analysis types.

#[derive(Deserialize, Debug, Clone)]
pub struct ByteRewrite {
    pub byte_start: usize,
    pub byte_end: usize,
    pub new_text: String,
    pub line: i64,
    pub col: i64,
    #[serde(default)]
    pub receiver_source: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SkippedMismatchSite {
    pub line: i64,
    pub col: i64,
    #[serde(default)]
    pub receiver_source: String,
    pub resolved_to_qpath: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct UnresolvedSite {
    pub line: i64,
    pub col: i64,
    #[serde(default)]
    pub receiver_source: String,
    pub why: String,
}

#[derive(Deserialize, Debug, Clone, Default)]
pub struct RenameAnalysis {
    #[serde(default)]
    pub rewrites: Vec<ByteRewrite>,
    #[serde(default)]
    pub skipped_mismatch: Vec<SkippedMismatchSite>,
    #[serde(default)]
    pub unresolved: Vec<UnresolvedSite>,
}

/// Go marshals a nil slice as JSON `null`, which `#[serde(default)]` alone
/// (absent-key only) won't tolerate — map an explicit null to the default.
fn null_as_default<'de, D, T>(d: D) -> std::result::Result<T, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(d)?.unwrap_or_default())
}

/// One interface whose contract the rename would break — surfaced by the
/// Go engine (project-wide go/types analysis), never auto-renamed.
#[derive(Deserialize, Debug, Clone)]
pub struct AffectedInterface {
    pub interface_qpath: String,
    pub method_qpath: String,
    pub file: String,
    pub line: i64,
}

/// Per-file result of a Go project-wide rename. Same three buckets as
/// RenameAnalysis plus optional interface impacts, which the daemon
/// stamps onto a single file entry (the declaring file).
#[derive(Deserialize, Debug, Clone, Default)]
pub struct RenameFileAnalysis {
    #[serde(default, deserialize_with = "null_as_default")]
    pub rewrites: Vec<ByteRewrite>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub skipped_mismatch: Vec<SkippedMismatchSite>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub unresolved: Vec<UnresolvedSite>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub affected_interfaces: Vec<AffectedInterface>,
}

#[derive(Deserialize, Debug, Clone)]
pub struct ParseResult {
    pub ok: bool,
    #[serde(default)]
    pub error_line: Option<i64>,
    #[serde(default)]
    pub error_message: Option<String>,
}

// ---------------------------------------------------------- adapters

pub struct Adapter {
    daemon: Daemon,
    pub language: String,
    project_root: PathBuf,
}

/// Port of GoAstAdapter.module_prefix: nearest go.mod's `module` declaration
/// plus the directory path relative to it; directory-layout fallback when no
/// go.mod ancestor exists. Cached per directory.
fn go_module_prefix(path: &Path, project_root: &Path) -> String {
    static CACHE: std::sync::LazyLock<std::sync::Mutex<HashMap<PathBuf, String>>> =
        std::sync::LazyLock::new(Default::default);

    let directory = path
        .parent()
        .map(|d| d.canonicalize().unwrap_or_else(|_| d.to_owned()))
        .unwrap_or_default();
    if let Some(hit) = CACHE.lock().unwrap().get(&directory) {
        return hit.clone();
    }

    let mut module: Option<(PathBuf, String)> = None;
    let mut current = directory.clone();
    loop {
        let gomod = current.join("go.mod");
        if gomod.is_file() {
            if let Some(name) = parse_go_module_name(&gomod) {
                module = Some((current.clone(), name));
            }
            break;
        }
        match current.parent() {
            Some(p) if p != current => current = p.to_owned(),
            _ => break,
        }
    }

    let prefix = match module {
        Some((module_root, module_name)) => {
            let rel = directory
                .strip_prefix(&module_root)
                .map(|r| r.to_string_lossy().replace(std::path::MAIN_SEPARATOR, "/"))
                .unwrap_or_default();
            let rel: String = rel
                .split('/')
                .filter(|p| !p.is_empty() && *p != ".")
                .collect::<Vec<_>>()
                .join("/");
            if rel.is_empty() {
                module_name
            } else {
                format!("{module_name}/{rel}")
            }
        }
        None => directory
            .strip_prefix(project_root)
            .map(|r| {
                r.to_string_lossy()
                    .replace(std::path::MAIN_SEPARATOR, "/")
                    .split('/')
                    .filter(|p| !p.is_empty() && *p != ".")
                    .collect::<Vec<_>>()
                    .join("/")
            })
            .unwrap_or_default(),
    };
    CACHE.lock().unwrap().insert(directory, prefix.clone());
    prefix
}

/// module is the first directive in a valid go.mod; skip blanks/comments.
fn parse_go_module_name(gomod: &Path) -> Option<String> {
    let text = std::fs::read_to_string(gomod).ok()?;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with("//") {
            continue;
        }
        if let Some(rest) = line.strip_prefix("module ") {
            let name = rest.trim().trim_matches('"');
            return (!name.is_empty()).then(|| name.to_owned());
        }
        break;
    }
    None
}

impl Adapter {
    pub fn scan_file(&self, path: &Path, module_prefix: Option<&str>) -> Result<FileScan> {
        let mut params = serde_json::json!({ "path": path.to_string_lossy() });
        // pyscan self-computes its module prefix (it owns the Python layout
        // rules); the Go prefix mirrors GoAstAdapter.module_prefix here.
        if let Some(mp) = module_prefix {
            params["module_prefix"] = mp.into();
        } else if self.language == "go" {
            params["module_prefix"] = go_module_prefix(path, &self.project_root).into();
        }
        // go-scan takes source inline; pyscan reads from disk when absent.
        if self.language == "go" {
            let source = std::fs::read(path)?;
            params["source"] = String::from_utf8_lossy(&source).into_owned().into();
        }
        let result = self.daemon.call("scan_file", params)?;
        Ok(serde_json::from_value(result)?)
    }

    pub fn scan_content(&self, source: &str) -> Result<FileScan> {
        let result = self
            .daemon
            .call("scan_content", serde_json::json!({ "source": source }))?;
        Ok(serde_json::from_value(result)?)
    }

    pub fn validate_syntax(&self, source: &str) -> Result<ParseResult> {
        let result = self
            .daemon
            .call("validate_syntax", serde_json::json!({ "source": source }))?;
        Ok(serde_json::from_value(result)?)
    }

    pub fn signature(&self, text: &str) -> Result<String> {
        let result = self
            .daemon
            .call("signature", serde_json::json!({ "text": text }))?;
        Ok(result["signature"].as_str().unwrap_or_default().to_owned())
    }

    pub fn preview(&self, body: &str, signature: &str, max_lines: usize) -> Result<String> {
        let result = self.daemon.call(
            "preview",
            serde_json::json!({ "body": body, "signature": signature, "max_lines": max_lines }),
        )?;
        Ok(result["preview"].as_str().unwrap_or_default().to_owned())
    }

    /// v2 engine rename analysis for one file. `method` is one of
    /// "rename_member" / "rename_field" / "rename_module_binding";
    /// `owner_or_module_qpath` maps to target_owner_qpath (member/field) or
    /// target_module_qpath (binding).
    pub fn rename_analysis(
        &self,
        method: &str,
        path: &Path,
        leaf: &str,
        target_qpath: &str,
        owner_or_module_qpath: &str,
        is_declaring_file: bool,
        decl_byte_range: Option<(i64, i64)>,
        new_name: &str,
    ) -> Result<RenameAnalysis> {
        let mut params = serde_json::json!({
            "path": path.to_string_lossy(),
            "leaf": leaf,
            "target_qpath": target_qpath,
            "is_declaring_file": is_declaring_file,
            "new_name": new_name,
        });
        if let Some((s, e)) = decl_byte_range {
            params["decl_byte_range"] = serde_json::json!([s, e]);
        }
        match method {
            "rename_member" | "rename_field" => {
                params["target_owner_qpath"] = owner_or_module_qpath.into();
            }
            "rename_module_binding" => {
                params["target_module_qpath"] = owner_or_module_qpath.into();
            }
            other => bail!("unknown rename method {other:?}"),
        }
        let result = self.daemon.call(method, params)?;
        Ok(serde_json::from_value(result)?)
    }

    /// v2 parameter-rename analysis for one file. Mirrors
    /// `PythonAstAdapter.rename_parameter`: the def slot + in-scope body uses
    /// (declaring file only) plus keyword-argument call sites (all files).
    #[allow(clippy::too_many_arguments)]
    pub fn rename_parameter_analysis(
        &self,
        path: &Path,
        func_qpath: &str,
        func_leaf: &str,
        param: &str,
        is_declaring_file: bool,
        decl_byte_range: Option<(i64, i64)>,
        new_name: &str,
    ) -> Result<RenameAnalysis> {
        let mut params = serde_json::json!({
            "path": path.to_string_lossy(),
            "func_qpath": func_qpath,
            "func_leaf": func_leaf,
            "param": param,
            "is_declaring_file": is_declaring_file,
            "new_name": new_name,
        });
        if let Some((s, e)) = decl_byte_range {
            params["decl_byte_range"] = serde_json::json!([s, e]);
        }
        let result = self.daemon.call("rename_parameter", params)?;
        Ok(serde_json::from_value(result)?)
    }

    /// Ordered parameter names of the function at `func_qpath`, or None when
    /// the def can't be located. Drives collision validation.
    pub fn function_parameters(
        &self,
        path: &Path,
        func_qpath: &str,
        decl_byte_range: Option<(i64, i64)>,
    ) -> Result<Option<Vec<String>>> {
        let mut params = serde_json::json!({
            "path": path.to_string_lossy(),
            "func_qpath": func_qpath,
        });
        if let Some((s, e)) = decl_byte_range {
            params["decl_byte_range"] = serde_json::json!([s, e]);
        }
        let result = self.daemon.call("function_parameters", params)?;
        Ok(serde_json::from_value(
            result.get("parameters").cloned().unwrap_or(serde_json::Value::Null),
        )?)
    }

    /// Go v2 engine rename: ONE project-wide daemon call (the go-scan
    /// daemon loads the whole project via go/packages itself — no per-file
    /// calls, no index callbacks). `container_qpath` maps to
    /// target_owner_qpath (member) or target_module_qpath (module binding).
    /// Returns the per-file analyses keyed by repo-relative path.
    #[allow(clippy::too_many_arguments)]
    pub fn go_rename(
        &self,
        member: bool,
        project_root: &Path,
        leaf: &str,
        new_name: &str,
        target_qpath: &str,
        container_qpath: &str,
        declaring_file: &str,
        decl_byte_range: (i64, i64),
    ) -> Result<HashMap<String, RenameFileAnalysis>> {
        let mut params = serde_json::json!({
            "project_root": project_root.to_string_lossy(),
            "leaf": leaf,
            "new_name": new_name,
            "target_qpath": target_qpath,
            "declaring_file": declaring_file,
            "decl_byte_range": [decl_byte_range.0, decl_byte_range.1],
            "candidate_files": [],
        });
        let method = if member {
            params["target_owner_qpath"] = container_qpath.into();
            "rename_member"
        } else {
            params["target_module_qpath"] = container_qpath.into();
            "rename_module_binding"
        };
        let result = self.daemon.call(method, params)?;
        let files = result
            .get("files")
            .cloned()
            .unwrap_or_else(|| serde_json::json!({}));
        Ok(serde_json::from_value(files)?)
    }

    /// insert-symbol: topmost decorator line of the def/class at `def_line`,
    /// or `def_line` itself when there are no decorators / no match / parse
    /// failure. Port of insert_symbol.py `_first_decorator_line`.
    pub fn first_decorator_line(&self, path: &Path, def_line: i64) -> Result<i64> {
        let result = self.daemon.call(
            "first_decorator_line",
            serde_json::json!({ "path": path.to_string_lossy(), "def_line": def_line }),
        )?;
        Ok(result["line"].as_i64().unwrap_or(def_line))
    }

    pub fn normalize_blank_gaps(&self, source: &str) -> Result<String> {
        let result = self
            .daemon
            .call("normalize_blank_gaps", serde_json::json!({ "source": source }))?;
        Ok(result["source"].as_str().unwrap_or_default().to_owned())
    }
}

// ---------------------------------------------------------- registry

pub struct Registry {
    project_root: PathBuf,
    adapters: std::sync::Mutex<HashMap<String, &'static Adapter>>,
}

/// The symbol repo checkout that provides pyscan (via uv) and go-scan.
/// Override with SYMBOL_REPO; defaults to this crate's own repo (dev mode).
fn symbol_repo() -> PathBuf {
    std::env::var("SYMBOL_REPO")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../..")
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        })
}

fn pyscan_argv() -> Vec<String> {
    if let Ok(cmd) = std::env::var("SYMBOL_PYSCAN") {
        return cmd.split_whitespace().map(str::to_owned).collect();
    }
    let repo = symbol_repo();
    vec![
        "uv".into(),
        "run".into(),
        "--project".into(),
        repo.to_string_lossy().into_owned(),
        "python".into(),
        "-m".into(),
        "wyolet.symbol.adapters.pyscan".into(),
    ]
}

fn goscan_argv() -> Option<Vec<String>> {
    if let Ok(cmd) = std::env::var("SYMBOL_GOSCAN") {
        return Some(cmd.split_whitespace().map(str::to_owned).collect());
    }
    let repo = symbol_repo();
    // Dev binaries a contributor may have `go build`'t. Mirrors the Python
    // adapter's dev path (adapters/go_ast/daemon/go-scan) so both engines
    // pick up the same freshly-built daemon before falling back to the
    // (CI-built, possibly older) bundled binaries below.
    for dev in [
        repo.join("daemon/go-scan"),
        repo.join("src/wyolet/symbol/adapters/go_ast/daemon/go-scan"),
    ] {
        if dev.is_file() {
            return Some(vec![dev.to_string_lossy().into_owned()]);
        }
    }
    let system = std::env::consts::OS; // "macos" → python platform says "darwin"
    let system = if system == "macos" { "darwin" } else { system };
    let arch = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    };
    let bundled = repo.join(format!("src/wyolet/symbol/bin/go-scan-{system}-{arch}"));
    bundled
        .is_file()
        .then(|| vec![bundled.to_string_lossy().into_owned()])
}

impl Registry {
    pub fn new(project_root: &Path) -> Registry {
        Registry {
            project_root: project_root.to_owned(),
            adapters: std::sync::Mutex::new(HashMap::new()),
        }
    }

    /// Languages with a registered adapter — mirrors registry.has_adapter.
    pub fn has_adapter(&self, lang_key: &str) -> bool {
        match lang_key {
            "python" => true,
            "go" => goscan_argv().is_some(),
            _ => false,
        }
    }

    /// Get (spawning lazily) the daemon adapter for a language key.
    /// Adapters are leaked to 'static: they live for the process and their
    /// Drop (shutdown notification) is best-effort via libc exit anyway.
    pub fn for_language(&self, lang_key: &str) -> Result<&'static Adapter> {
        {
            let map = self.adapters.lock().unwrap();
            if let Some(a) = map.get(lang_key) {
                return Ok(a);
            }
        }
        let (name, argv) = match lang_key {
            "python" => ("pyscan", pyscan_argv()),
            "go" => (
                "go-scan",
                goscan_argv().ok_or_else(|| anyhow::anyhow!("go-scan binary not found"))?,
            ),
            other => bail!("no adapter for language {other:?}"),
        };
        let init = serde_json::json!({
            "protocol_version": "1",
            "project_root": self.project_root.to_string_lossy(),
        });
        let adapter: &'static Adapter = Box::leak(Box::new(Adapter {
            daemon: Daemon::spawn(name, &argv, init)?,
            language: lang_key.to_owned(),
            project_root: self.project_root.clone(),
        }));
        self.adapters
            .lock()
            .unwrap()
            .insert(lang_key.to_owned(), adapter);
        Ok(adapter)
    }
}

static REGISTRY: OnceLock<Registry> = OnceLock::new();

pub fn default_registry(project_root: &Path) -> &'static Registry {
    REGISTRY.get_or_init(|| Registry::new(project_root))
}
