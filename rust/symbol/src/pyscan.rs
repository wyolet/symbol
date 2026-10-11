//! The Python daemon (`wyolet.symbol.adapters.pyscan`) ships inside the
//! binary: its stdlib-only import closure is embedded at compile time,
//! written once to a content-addressed cache dir, and run by a `python3`
//! found on PATH. Python projects need an interpreter, nothing installed.

use std::fmt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use anyhow::{Context, Result};

/// Interpreter override (path or command name).
pub const PYTHON_ENV: &str = "SYMBOL_PYTHON";
/// Full daemon command override (whitespace-split).
pub const PYSCAN_ENV: &str = "SYMBOL_PYSCAN";
/// Oldest interpreter the embedded code runs on (PEP 604 unions are
/// evaluated at runtime in its annotations and dataclasses).
pub const MIN_PYTHON: (u32, u32) = (3, 11);

macro_rules! embed {
    ($($rel:literal),* $(,)?) => {
        &[$(($rel, include_str!(concat!("../../../src/", $rel)))),*]
    };
}

// adapters/__init__.py is left out on purpose: it eagerly registers every
// adapter (go_ast, dependency hooks) and drags in non-stdlib modules.
// Without it the directory imports as a namespace package.
const FILES: &[(&str, &str)] = embed!(
    "wyolet/symbol/__init__.py",
    "wyolet/symbol/adapters/_python_ast_rename.py",
    "wyolet/symbol/adapters/pyscan.py",
    "wyolet/symbol/adapters/python_ast.py",
    "wyolet/symbol/adapters/python_deps.py",
    "wyolet/symbol/protocols/__init__.py",
    "wyolet/symbol/protocols/index_query.py",
    "wyolet/symbol/protocols/language.py",
    "wyolet/symbol/protocols/read_cache.py",
    "wyolet/symbol/protocols/types.py",
    "wyolet/symbol/shared/__init__.py",
    "wyolet/symbol/shared/deps.py",
    "wyolet/symbol/shared/findings.py",
    "wyolet/symbol/shared/pipeline.py",
    "wyolet/symbol/shared/pkg_registry.py",
    "wyolet/symbol/shared/spec.py",
    "wyolet/symbol/writes/__init__.py",
    "wyolet/symbol/writes/_blank_lines.py",
);

const BOOTSTRAP: &str = "import sys; sys.path.insert(0, sys.argv[1]); \
     from wyolet.symbol.adapters.pyscan import main; main()";

/// No usable interpreter. Display is a JSON object so callers and scripts
/// can read the requirement and the override from the error text.
#[derive(Debug)]
pub struct PythonUnavailable {
    /// (interpreter, what happened)
    pub tried: Vec<(String, String)>,
}

impl fmt::Display for PythonUnavailable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.to_json())
    }
}

impl PythonUnavailable {
    pub fn to_json(&self) -> serde_json::Value {
        let required = format!("{}.{}", MIN_PYTHON.0, MIN_PYTHON.1);
        let tried: Vec<String> = self
            .tried
            .iter()
            .map(|(p, why)| format!("{p}: {why}"))
            .collect();
        serde_json::json!({
            "status": "error",
            "error_code": "python_unavailable",
            "message": format!(
                "Python analysis needs Python >= {required} ({}); install it or set {PYTHON_ENV} to its interpreter",
                tried.join("; ")
            ),
            "required_python": format!(">={required}"),
            "override_env": PYTHON_ENV,
            "tried": self.tried.iter()
                .map(|(p, why)| serde_json::json!({"interpreter": p, "result": why}))
                .collect::<Vec<_>>(),
        })
    }
}

impl std::error::Error for PythonUnavailable {}

/// Command that starts the daemon: `SYMBOL_PYSCAN` verbatim, else the
/// embedded module under a probed interpreter.
pub fn argv() -> Result<Vec<String>> {
    if let Ok(cmd) = std::env::var(PYSCAN_ENV) {
        return Ok(cmd.split_whitespace().map(str::to_owned).collect());
    }
    let candidates = match std::env::var(PYTHON_ENV) {
        Ok(p) if !p.is_empty() => vec![p],
        _ => vec!["python3".to_owned(), "python".to_owned()],
    };
    let python = find_python(&candidates)?;
    // Read-only home (containers, sandboxes): fall back to the temp dir.
    let dir = materialize(&cache_root())
        .or_else(|_| materialize(&std::env::temp_dir().join("symbol")))?;
    Ok(embedded_argv(&python, &dir))
}

fn embedded_argv(python: &str, dir: &Path) -> Vec<String> {
    // -I -S: no user env, cwd or site-packages can shadow the embedded
    // modules. -X utf8: the JSON stream is UTF-8 whatever the locale.
    [python, "-I", "-S", "-X", "utf8", "-c", BOOTSTRAP]
        .iter()
        .map(|s| s.to_string())
        .chain([dir.to_string_lossy().into_owned()])
        .collect()
}

/// First candidate that runs and is at least MIN_PYTHON.
pub fn find_python(candidates: &[String]) -> std::result::Result<String, PythonUnavailable> {
    let mut tried = vec![];
    for python in candidates {
        match probe_version(python) {
            Ok((major, minor, _)) if (major, minor) >= MIN_PYTHON => return Ok(python.clone()),
            Ok((_, _, full)) => tried.push((python.clone(), format!("found {full}, too old"))),
            Err(why) => tried.push((python.clone(), why)),
        }
    }
    Err(PythonUnavailable { tried })
}

fn probe_version(python: &str) -> std::result::Result<(u32, u32, String), String> {
    let out = Command::new(python)
        .args(["-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => "not found".to_owned(),
            _ => format!("failed to start: {e}"),
        })?;
    let text = String::from_utf8_lossy(&out.stdout).trim().to_owned();
    let mut parts = text.split('.').map(|p| p.parse::<u32>());
    match (out.status.success(), parts.next(), parts.next()) {
        (true, Some(Ok(major)), Some(Ok(minor))) => Ok((major, minor, text)),
        _ => Err("did not report a Python version".to_owned()),
    }
}

fn cache_root() -> PathBuf {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let base = if let Some(xdg) = std::env::var_os("XDG_CACHE_HOME") {
        Some(PathBuf::from(xdg))
    } else if cfg!(target_os = "macos") {
        home.map(|h| h.join("Library/Caches"))
    } else if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
    } else {
        home.map(|h| h.join(".cache"))
    };
    base.unwrap_or_else(std::env::temp_dir).join("symbol")
}

fn content_hash() -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for (rel, text) in FILES {
        for &b in rel
            .as_bytes()
            .iter()
            .chain([0u8].iter())
            .chain(text.as_bytes())
        {
            h ^= b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
    }
    format!("{h:016x}")
}

/// Write the embedded files under `root/pyscan-<hash>/` unless already
/// there. Each file lands via rename, so a concurrent process never
/// imports a half-written module.
pub fn materialize(root: &Path) -> Result<PathBuf> {
    let dir = root.join(format!("pyscan-{}", content_hash()));
    for (rel, text) in FILES {
        let target = dir.join(rel);
        if target.is_file() {
            continue;
        }
        let parent = target.parent().unwrap_or(&dir);
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating pyscan cache dir {}", parent.display()))?;
        let tmp = target.with_extension(format!("tmp-{}", std::process::id()));
        std::fs::write(&tmp, text).with_context(|| format!("writing {}", tmp.display()))?;
        std::fs::rename(&tmp, &target).with_context(|| format!("writing {}", target.display()))?;
    }
    Ok(dir)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};

    #[test]
    fn missing_interpreter_is_a_structured_error() {
        let err = find_python(&["/nonexistent/python3".to_owned()]).unwrap_err();
        let body: serde_json::Value = serde_json::from_str(&err.to_string()).unwrap();
        assert_eq!(body["error_code"], "python_unavailable");
        assert_eq!(body["override_env"], PYTHON_ENV);
        assert_eq!(body["required_python"], ">=3.11");
        assert_eq!(body["tried"][0]["result"], "not found");
    }

    #[cfg(unix)]
    #[test]
    fn old_interpreter_is_rejected() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = tempfile::tempdir().unwrap();
        let fake = tmp.path().join("python3");
        std::fs::write(&fake, "#!/bin/sh\necho 3.9.6\n").unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        let err = find_python(&[fake.to_string_lossy().into_owned()]).unwrap_err();
        assert!(err.to_string().contains("found 3.9.6, too old"), "{err}");
    }

    #[test]
    fn materialize_is_idempotent() {
        let tmp = tempfile::tempdir().unwrap();
        let first = materialize(tmp.path()).unwrap();
        let second = materialize(tmp.path()).unwrap();
        assert_eq!(first, second);
        for (rel, text) in FILES {
            assert_eq!(std::fs::read_to_string(first.join(rel)).unwrap(), *text);
        }
    }

    /// One request against the embedded set alone (no site-packages).
    fn embedded_call(request: &str) -> serde_json::Value {
        let python = find_python(&["python3".to_owned()]).expect("python3 >= 3.11 on PATH");
        let tmp = tempfile::tempdir().unwrap();
        let dir = materialize(tmp.path()).unwrap();
        let argv = embedded_argv(&python, &dir);
        let mut child = Command::new(&argv[0])
            .args(&argv[1..])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdin = child.stdin.take().unwrap();
        let mut stdout = BufReader::new(child.stdout.take().unwrap());
        writeln!(stdin, "{request}").unwrap();
        let mut line = String::new();
        stdout.read_line(&mut line).unwrap();
        drop(stdin);
        let _ = child.wait();
        serde_json::from_str(&line).unwrap_or_else(|e| panic!("{e}: {line}"))
    }

    #[test]
    fn embedded_daemon_answers_without_site_packages() {
        let resp = embedded_call(
            r#"{"jsonrpc":"2.0","id":1,"method":"scan_content","params":{"source":"class A:\n    def f(self): pass\n"}}"#,
        );
        assert_eq!(
            resp["result"]["symbols"][0]["qualified_path"], "A",
            "{resp}"
        );
    }

    #[test]
    fn embedded_daemon_reports_project_dependencies() {
        let project = tempfile::tempdir().unwrap();
        std::fs::write(
            project.path().join("pyproject.toml"),
            "[project]\nname = \"x\"\ndependencies = [\"FastAPI>=0.100\", \"Flask_Login\"]\n",
        )
        .unwrap();
        std::fs::write(project.path().join("requirements.txt"), "requests==2.0\n").unwrap();
        let request = serde_json::json!({
            "jsonrpc": "2.0", "id": 1, "method": "project_dependencies",
            "params": {"project_root": project.path()},
        });
        let resp = embedded_call(&request.to_string());
        assert_eq!(
            resp["result"]["dependencies"],
            serde_json::json!(["fastapi", "flask-login", "requests"]),
            "{resp}"
        );
    }
}
