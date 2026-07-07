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
}

impl Adapter {
    pub fn scan_file(&self, path: &Path, module_prefix: Option<&str>) -> Result<FileScan> {
        let mut params = serde_json::json!({ "path": path.to_string_lossy() });
        if let Some(mp) = module_prefix {
            params["module_prefix"] = mp.into();
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
    let dev = repo.join("daemon/go-scan");
    if dev.is_file() {
        return Some(vec![dev.to_string_lossy().into_owned()]);
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
