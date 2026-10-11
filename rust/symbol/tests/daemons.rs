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
    Command::new(bin)
        .env_remove("SYMBOL_GOSCAN")
        .env_remove("SYMBOL_PYSCAN")
        .env("SYMBOL_REPO", "/nonexistent")
        .env("SYMBOL_SESSION_ID", "daemon-tests")
        .arg("--root")
        .arg(root)
        .arg("--json")
        .args(args)
        .output()
        .expect("spawn symbol")
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
    let bin_dir = tempfile::tempdir().unwrap();
    let bin = bin_dir.path().join("symbol");
    std::fs::copy(env!("CARGO_BIN_EXE_symbol"), &bin).unwrap();
    std::fs::copy(&goscan, bin_dir.path().join("go-scan")).unwrap();

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
