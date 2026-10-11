//! Daemon discovery and missing-daemon behavior, driven through the compiled
//! binary with every discovery env var pointed away from this checkout.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn dev_goscan() -> Option<PathBuf> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/wyolet/symbol/adapters/go_ast/daemon/go-scan");
    path.is_file().then_some(path)
}

fn go_project() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("go.mod"),
        "module example.com/demo\n\ngo 1.21\n",
    )
    .unwrap();
    std::fs::write(
        dir.path().join("demo.go"),
        "package demo\n\nfunc Hello() string {\n\treturn \"hi\"\n}\n",
    )
    .unwrap();
    dir
}

/// Run `bin` with no daemon overrides and SYMBOL_REPO pointing nowhere, so
/// only the executable's own directory can supply go-scan.
fn run_isolated(bin: &Path, root: &Path, args: &[&str]) -> Output {
    run_with_env(bin, root, args, &[])
}

fn run_with_env(bin: &Path, root: &Path, args: &[&str], env: &[(&str, &str)]) -> Output {
    Command::new(bin)
        .env_remove("SYMBOL_GOSCAN")
        .env_remove("SYMBOL_PYSCAN")
        .env("SYMBOL_REPO", "/nonexistent")
        .env("SYMBOL_SESSION_ID", "daemon-tests")
        .envs(env.iter().copied())
        .arg("--root")
        .arg(root)
        .arg("--json")
        .args(args)
        .output()
        .expect("spawn symbol")
}

/// A copy of the binary alone in its own directory, optionally with go-scan.
fn install(goscan: Option<&Path>) -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let bin = dir.path().join("symbol");
    std::fs::copy(env!("CARGO_BIN_EXE_symbol"), &bin).unwrap();
    if let Some(goscan) = goscan {
        std::fs::copy(goscan, dir.path().join("go-scan")).unwrap();
    }
    (dir, bin)
}

fn index_path(root: &Path) -> PathBuf {
    root.join(".symbol/symbol_index.msgpack.zst")
}

fn assert_daemon_unavailable(out: &Output, language: &str, env_var: &str) {
    assert!(!out.status.success(), "expected failure: {out:?}");
    let err = stdout_json(out);
    assert_eq!(err["error_code"], "daemon_unavailable", "{err}");
    assert_eq!(err["language"], language, "{err}");
    assert_eq!(err["env_var"], env_var, "{err}");
}

fn stdout_json(out: &Output) -> serde_json::Value {
    let stdout = String::from_utf8_lossy(&out.stdout);
    serde_json::from_str(&stdout).unwrap_or_else(|e| {
        panic!(
            "invalid symbol JSON (status {})\nerror: {e}\nstdout: {stdout}\nstderr: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        )
    })
}

#[cfg(unix)]
#[test]
fn goscan_found_next_to_executable() {
    let Some(goscan) = dev_goscan() else {
        eprintln!("skipped: go-scan dev build missing (make build-go-scan)");
        return;
    };
    let (_bin_dir, bin) = install(Some(&goscan));

    let project = go_project();
    let out = run_isolated(&bin, project.path(), &["index"]);
    assert!(out.status.success(), "index failed: {out:?}");
    assert_eq!(stdout_json(&out)["stats"]["files"], 1);

    // A symlink onto PATH resolves back to the real install directory.
    let link_dir = tempfile::tempdir().unwrap();
    let link = link_dir.path().join("symbol");
    std::os::unix::fs::symlink(&bin, &link).unwrap();
    let out = run_isolated(&link, project.path(), &["search", "Hello"]);
    assert!(out.status.success(), "search failed: {out:?}");
    let hits = stdout_json(&out);
    assert_eq!(hits[0]["path"], "example.com/demo.Hello", "{hits}");
}

#[test]
fn index_fails_when_goscan_missing() {
    let (_bin_dir, bin) = install(None);
    let project = go_project();

    let out = run_isolated(&bin, project.path(), &["index"]);
    assert_daemon_unavailable(&out, "go", "SYMBOL_GOSCAN");
    assert!(!index_path(project.path()).exists());
}

#[test]
fn bad_goscan_override_fails() {
    let (_bin_dir, bin) = install(None);
    let project = go_project();

    let out = run_with_env(
        &bin,
        project.path(),
        &["index"],
        &[("SYMBOL_GOSCAN", "/nonexistent/go-scan")],
    );
    assert_daemon_unavailable(&out, "go", "SYMBOL_GOSCAN");
}

#[test]
fn index_fails_when_pyscan_missing() {
    let (_bin_dir, bin) = install(None);
    let project = tempfile::tempdir().unwrap();
    std::fs::write(project.path().join("app.py"), "def run():\n    pass\n").unwrap();

    let out = run_with_env(
        &bin,
        project.path(),
        &["index"],
        &[("SYMBOL_PYSCAN", "/nonexistent/pyscan")],
    );
    assert_daemon_unavailable(&out, "python", "SYMBOL_PYSCAN");
    assert!(!index_path(project.path()).exists());
}

#[test]
fn refresh_keeps_index_when_goscan_missing() {
    let Some(goscan) = dev_goscan() else {
        eprintln!("skipped: go-scan dev build missing (make build-go-scan)");
        return;
    };
    let (bin_dir, bin) = install(Some(&goscan));
    let project = go_project();
    let out = run_isolated(&bin, project.path(), &["index"]);
    assert!(out.status.success(), "index failed: {out:?}");
    let saved = std::fs::read(index_path(project.path())).unwrap();

    std::fs::remove_file(bin_dir.path().join("go-scan")).unwrap();
    std::fs::write(
        project.path().join("demo.go"),
        "package demo\n\nfunc Hello() string {\n\treturn \"hello\"\n}\n",
    )
    .unwrap();

    for args in [&["refresh"][..], &["search", "Hello"]] {
        let out = run_isolated(&bin, project.path(), args);
        assert_daemon_unavailable(&out, "go", "SYMBOL_GOSCAN");
        assert_eq!(
            std::fs::read(index_path(project.path())).unwrap(),
            saved,
            "{args:?} rewrote the index"
        );
    }
}
