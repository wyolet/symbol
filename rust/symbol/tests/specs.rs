//! Per-package spec excludes reach the index build: the fastapi spec drops
//! `docs_src/**`, but only for projects that declare fastapi.

use std::path::Path;
use std::process::Command;

fn indexed_files(root: &Path) -> serde_json::Value {
    let out = Command::new(env!("CARGO_BIN_EXE_symbol"))
        .arg("--root")
        .arg(root)
        .arg("--json")
        .arg("index")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    v["stats"]["files"].clone()
}

fn project(dependencies: &str) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    std::fs::create_dir_all(root.join("docs_src/tutorial")).unwrap();
    std::fs::write(
        root.join("pyproject.toml"),
        format!("[project]\nname = \"svc\"\nversion = \"0\"\ndependencies = [{dependencies}]\n"),
    )
    .unwrap();
    std::fs::write(root.join("main.py"), "def serve():\n    return 1\n").unwrap();
    std::fs::write(
        root.join("docs_src/tutorial/first.py"),
        "def tutorial_step():\n    return 1\n",
    )
    .unwrap();
    dir
}

#[test]
fn declared_dependency_applies_its_spec_excludes() {
    let with = project("\"FastAPI>=0.100\"");
    assert_eq!(indexed_files(with.path()), 1);
    let without = project("\"requests\"");
    assert_eq!(indexed_files(without.path()), 2);
}
