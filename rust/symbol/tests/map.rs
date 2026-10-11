//! `symbol map` against throwaway Python and Go projects, through the real
//! pyscan / go-scan daemons.

use std::path::Path;
use std::process::Command;

fn write(root: &Path, files: &[(&str, &str)]) {
    for (rel, text) in files {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
}

fn map(root: &Path, args: &[&str]) -> (bool, serde_json::Value) {
    let out = Command::new(env!("CARGO_BIN_EXE_symbol"))
        .env("SYMBOL_SESSION_ID", "map-tests")
        .arg("--root")
        .arg(root)
        .arg("--json")
        .arg("map")
        .args(args)
        .output()
        .expect("spawn symbol");
    let stdout = String::from_utf8_lossy(&out.stdout);
    let json = serde_json::from_str(&stdout).unwrap_or_else(|e| {
        panic!(
            "invalid JSON ({e})\nstdout: {stdout}\nstderr: {}",
            String::from_utf8_lossy(&out.stderr)
        )
    });
    (out.status.success(), json)
}

fn goscan_available() -> bool {
    let dev = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/wyolet/symbol/adapters/go_ast/daemon/go-scan");
    if dev.is_file() || std::env::var_os("SYMBOL_GOSCAN").is_some() {
        return true;
    }
    eprintln!("skipped: go-scan dev build missing (make build-go-scan)");
    false
}

fn imports_of<'a>(out: &'a serde_json::Value, module: &str) -> Vec<&'a str> {
    out["modules"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["module"] == module)
        .unwrap_or_else(|| panic!("no module {module}: {out}"))["imports"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect()
}

fn closure(out: &serde_json::Value) -> Vec<(String, i64)> {
    out["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| {
            (
                m["module"].as_str().unwrap().to_owned(),
                m["depth"].as_i64().unwrap(),
            )
        })
        .collect()
}

#[test]
fn python_project_graph() {
    let dir = tempfile::tempdir().unwrap();
    write(
        dir.path(),
        &[
            (
                "pkg/a.py",
                "from pkg import b\n\ndef fa():\n    return b.fb()\n",
            ),
            ("pkg/b.py", "import pkg.a\n\ndef fb():\n    return 1\n"),
            (
                "pkg/c.py",
                "import os\nfrom pkg.b import fb\n\ndef fc():\n    return fb()\n",
            ),
            ("app.py", "from pkg.c import fc\n\ndef main():\n    fc()\n"),
        ],
    );
    let (ok, out) = map(dir.path(), &[]);
    assert!(ok, "{out}");
    assert_eq!(out["total_modules"], 4, "{out}");
    assert_eq!(imports_of(&out, "pkg.a"), ["pkg.b"]);
    assert_eq!(imports_of(&out, "pkg.c"), ["pkg.b"]);
    assert_eq!(
        out["cycles"][0]["modules"],
        serde_json::json!(["pkg.a", "pkg.b"])
    );
    assert_eq!(out["hotspots"][0]["module"], "pkg.b");
    assert_eq!(out["hotspots"][0]["fan_in"], 2);

    let (ok, deps) = map(dir.path(), &["--deps", "app"]);
    assert!(ok, "{deps}");
    assert_eq!(
        closure(&deps),
        [
            ("pkg.c".to_owned(), 1),
            ("pkg.b".to_owned(), 2),
            ("pkg.a".to_owned(), 3)
        ]
    );
}

#[test]
fn go_nested_modules_without_workspace() {
    if !goscan_available() {
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    write(
        dir.path(),
        &[
            ("go.mod", "module example.com/app\n\ngo 1.21\n"),
            (
                "main.go",
                "package main\n\nimport \"example.com/app/pkg/gateway/client\"\n\nfunc main() { client.New() }\n",
            ),
            (
                "pkg/gateway/client/client.go",
                "package client\n\nimport (\n\t\"net/http\"\n\n\t\"example.com/app/apiclient/types\"\n)\n\nfunc New() types.T { _ = http.MethodGet; return types.T{} }\n",
            ),
            ("apiclient/go.mod", "module example.com/app/apiclient\n\ngo 1.21\n"),
            ("apiclient/types/types.go", "package types\n\ntype T struct{}\n"),
        ],
    );
    let (ok, out) = map(dir.path(), &[]);
    assert!(ok, "{out}");
    assert_eq!(
        imports_of(&out, "example.com/app/pkg/gateway/client"),
        ["example.com/app/apiclient/types"]
    );
    assert_eq!(out["external_sources"], 1, "{out}");

    let (ok, up) = map(dir.path(), &["--dependents", "apiclient/types"]);
    assert!(ok, "{up}");
    assert_eq!(up["target"], "example.com/app/apiclient/types");
    assert_eq!(
        closure(&up),
        [
            ("example.com/app/pkg/gateway/client".to_owned(), 1),
            ("example.com/app".to_owned(), 2)
        ]
    );
}

#[test]
fn go_workspace_modules_and_test_only_cycle() {
    if !goscan_available() {
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    write(
        dir.path(),
        &[
            ("go.work", "go 1.21\n\nuse .\n\nuse ./sdk\n"),
            ("go.mod", "module example.com/relay\n\ngo 1.21\n"),
            (
                "app/store/store.go",
                "package store\n\nimport \"example.com/relay/sdk/v1\"\n\nfunc Get() v1.Msg { return v1.Msg{} }\n",
            ),
            (
                "app/store/storetest/fake.go",
                "package storetest\n\nimport \"example.com/relay/app/store\"\n\nfunc Fake() { store.Get() }\n",
            ),
            (
                "app/store/store_test.go",
                "package store\n\nimport (\n\t\"testing\"\n\n\t\"example.com/relay/app/store/storetest\"\n)\n\nfunc TestGet(t *testing.T) { storetest.Fake() }\n",
            ),
            ("sdk/go.mod", "module example.com/relay/sdk\n\ngo 1.21\n"),
            ("sdk/v1/msg.go", "package v1\n\ntype Msg struct{}\n"),
        ],
    );
    let (ok, out) = map(dir.path(), &[]);
    assert!(ok, "{out}");
    let store = imports_of(&out, "example.com/relay/app/store");
    assert!(store.contains(&"example.com/relay/sdk/v1"), "{out}");
    let cycle = &out["cycles"][0];
    assert_eq!(
        cycle["modules"],
        serde_json::json!([
            "example.com/relay/app/store",
            "example.com/relay/app/store/storetest"
        ])
    );
    assert_eq!(
        cycle["edges"][0]["sites"][0]["file"],
        "app/store/store_test.go"
    );

    let (ok, prod) = map(dir.path(), &["--exclude", "*_test.go"]);
    assert!(ok, "{prod}");
    assert_eq!(prod["cycles"], serde_json::json!([]));

    let (ok, err) = map(dir.path(), &["--dependents", "nope/missing"]);
    assert!(!ok);
    assert_eq!(err["error_code"], "module_not_found");
}
