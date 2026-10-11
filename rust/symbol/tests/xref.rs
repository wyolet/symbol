//! `symbol refs` / `symbol implementers` through the compiled binary. The Go
//! cases need the go-scan dev build and skip without it.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn dev_goscan() -> Option<PathBuf> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/wyolet/symbol/adapters/go_ast/daemon/go-scan");
    path.is_file().then_some(path)
}

fn write(root: &Path, rel: &str, src: &str) {
    let abs = root.join(rel);
    std::fs::create_dir_all(abs.parent().unwrap()).unwrap();
    std::fs::write(abs, src).unwrap();
}

fn go_project() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write(root, "go.mod", "module example.com/shop\n\ngo 1.21\n");
    write(
        root,
        "store/store.go",
        "package store\n\
         \n\
         import \"net/http\"\n\
         \n\
         type Store struct{ Name string }\n\
         \n\
         func (s *Store) Save() error { return nil }\n\
         \n\
         type Memory struct{}\n\
         \n\
         func (Memory) Save() error { return nil }\n\
         \n\
         type Saver interface{ Save() error }\n\
         \n\
         type Handler struct{}\n\
         \n\
         func (*Handler) ServeHTTP(http.ResponseWriter, *http.Request) {}\n",
    );
    write(
        root,
        "api/api.go",
        "package api\n\
         \n\
         import \"example.com/shop/store\"\n\
         \n\
         func Persist(s *store.Store) error {\n\
         \x20\x20\x20\x20_ = s.Name\n\
         \x20\x20\x20\x20return s.Save()\n\
         }\n\
         \n\
         func Save() {}\n",
    );
    write(
        root,
        "api/api_test.go",
        "package api_test\n\
         \n\
         import (\n\
         \x20\x20\x20\x20\"testing\"\n\
         \n\
         \x20\x20\x20\x20\"example.com/shop/store\"\n\
         )\n\
         \n\
         func TestSave(t *testing.T) { _ = (&store.Store{}).Save() }\n",
    );
    dir
}

fn run(root: &Path, args: &[&str]) -> (Output, serde_json::Value) {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_symbol"));
    if let Some(goscan) = dev_goscan() {
        cmd.env("SYMBOL_GOSCAN", goscan);
    }
    let out = cmd
        .env("UV_CACHE_DIR", root.join(".uv-cache"))
        .env("SYMBOL_SESSION_ID", "xref-tests")
        .arg("--root")
        .arg(root)
        .arg("--json")
        .args(args)
        .output()
        .expect("spawn symbol");
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let json = serde_json::from_str(&stdout).unwrap_or_else(|e| {
        panic!(
            "invalid symbol JSON (status {})\nerror: {e}\nstdout: {stdout}\nstderr: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        )
    });
    (out, json)
}

fn sites(result: &serde_json::Value) -> Vec<String> {
    result["references"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            format!(
                "{}:{} {}",
                r["source_file"].as_str().unwrap(),
                r["ref_line"],
                r["source_path"].as_str().unwrap_or("-")
            )
        })
        .collect()
}

macro_rules! require_goscan {
    () => {
        if dev_goscan().is_none() {
            eprintln!("skipped: go-scan dev build missing (make build-go-scan)");
            return;
        }
    };
}

#[test]
fn refs_resolve_method_across_packages_and_tests() {
    require_goscan!();
    let project = go_project();
    let (out, result) = run(project.path(), &["refs", "store.Store.Save"]);
    assert!(out.status.success(), "{result}");
    assert_eq!(
        result["qualified_path"],
        "example.com/shop/store.Store.Save"
    );
    assert_eq!(result["target"]["kind"], "method");
    // Memory.Save and the unrelated api.Save are different objects.
    assert_eq!(
        sites(&result),
        [
            "api/api.go:7 example.com/shop/api.Persist",
            "api/api_test.go:9 example.com/shop/api.TestSave",
        ]
    );
}

#[test]
fn refs_reach_fields_through_their_type() {
    require_goscan!();
    let project = go_project();
    let (out, result) = run(project.path(), &["refs", "Store.Name"]);
    assert!(out.status.success(), "{result}");
    assert_eq!(result["target"]["kind"], "field");
    assert_eq!(
        sites(&result),
        ["api/api.go:6 example.com/shop/api.Persist"]
    );
}

#[test]
fn implementers_of_interface_and_dependency_interface() {
    require_goscan!();
    let project = go_project();
    let (out, result) = run(project.path(), &["implementers", "store.Saver"]);
    assert!(out.status.success(), "{result}");
    let found: Vec<(String, bool)> = result["implementers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| {
            (
                i["path"].as_str().unwrap().to_owned(),
                i["pointer"].as_bool().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        found,
        [
            ("example.com/shop/store.Memory".to_owned(), false),
            ("example.com/shop/store.Store".to_owned(), true),
        ]
    );

    let (out, result) = run(project.path(), &["implementers", "net/http.Handler"]);
    assert!(out.status.success(), "{result}");
    assert_eq!(result["target"]["external"], true);
    assert_eq!(
        result["implementers"][0]["path"],
        "example.com/shop/store.Handler"
    );

    let (out, result) = run(project.path(), &["implementers", "store.Handler"]);
    assert!(out.status.success(), "{result}");
    let interfaces: Vec<&str> = result["interfaces"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["path"].as_str().unwrap())
        .collect();
    assert!(interfaces.contains(&"net/http.Handler"), "{result}");
}

#[test]
fn ambiguous_path_lists_candidates() {
    require_goscan!();
    let project = go_project();
    let (out, result) = run(project.path(), &["refs", "Save"]);
    assert!(!out.status.success());
    assert_eq!(result["error_code"], "symbol_ambiguous", "{result}");
    assert_eq!(result["candidates"].as_array().unwrap().len(), 3);
}

#[test]
fn unknown_path_is_not_found() {
    require_goscan!();
    let project = go_project();
    let (out, result) = run(project.path(), &["implementers", "store.Missing"]);
    assert!(!out.status.success());
    assert_eq!(result["error_code"], "symbol_not_found", "{result}");
}

#[test]
fn type_errors_fail_loudly() {
    require_goscan!();
    let project = go_project();
    write(
        project.path(),
        "store/broken.go",
        "package store\n\nfunc broken() int { return missing }\n",
    );
    let (out, result) = run(project.path(), &["refs", "store.Store.Save"]);
    assert!(!out.status.success());
    assert_eq!(result["error_code"], "load_failed", "{result}");
    assert!(
        result["errors"][0].as_str().unwrap().contains("missing"),
        "{result}"
    );
}

#[test]
fn adapter_without_support_reports_unsupported() {
    let project = tempfile::tempdir().unwrap();
    write(project.path(), "app.py", "def run():\n    pass\n");
    let (out, result) = run(project.path(), &["refs", "app.run"]);
    assert!(!out.status.success());
    assert_eq!(result["error_code"], "unsupported", "{result}");
}
