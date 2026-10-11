//! End-to-end tests of the symbol-linguist binary: classification of a
//! fixture tree, JSON shape, and cache behavior (warm hit, invalidation on
//! touch, corrupt-cache cold start).

use std::path::Path;
use std::process::Command;

fn bin() -> Command {
    Command::new(env!("CARGO_BIN_EXE_symbol-linguist"))
}

fn make_tree(root: &Path) {
    std::fs::create_dir_all(root.join("src")).unwrap();
    std::fs::create_dir_all(root.join("node_modules/dep")).unwrap();
    std::fs::create_dir_all(root.join(".hidden")).unwrap();
    std::fs::write(root.join("src/main.py"), "import os\n\nprint(1)\n").unwrap();
    std::fs::write(root.join("src/lib.rs"), "fn main() {}\n").unwrap();
    std::fs::write(root.join("Makefile"), "all:\n\ttrue\n").unwrap();
    std::fs::write(root.join("script"), "#!/usr/bin/env python3\nprint(2)\n").unwrap();
    std::fs::write(root.join("data.bin"), [0u8, 1, 2, 3]).unwrap();
    std::fs::write(root.join("empty.py"), "").unwrap();
    std::fs::write(root.join("node_modules/dep/x.js"), "var x;\n").unwrap();
    std::fs::write(root.join(".hidden/y.py"), "print(3)\n").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(root.join("src/main.py"), root.join("link.py")).unwrap();
}

fn run_json(root: &Path, extra: &[&str]) -> serde_json::Value {
    let out = bin()
        .arg(root)
        .arg("--json")
        .args(extra)
        .output()
        .expect("binary runs");
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("valid json")
}

fn lang<'a>(v: &'a serde_json::Value, name: &str) -> Option<&'a serde_json::Value> {
    v["languages"]
        .as_array()
        .unwrap()
        .iter()
        .find(|l| l["name"] == name)
}

#[test]
fn classifies_fixture_tree() {
    let tmp = tempfile::tempdir().unwrap();
    make_tree(tmp.path());
    let v = run_json(tmp.path(), &["--no-cache"]);

    // src/main.py + script (shebang) = 2 Python files; empty.py is gated out
    assert_eq!(lang(&v, "Python").unwrap()["files"], 2);
    assert_eq!(lang(&v, "Rust").unwrap()["files"], 1);
    assert_eq!(lang(&v, "Makefile").unwrap()["files"], 1);
    // vendored/hidden/binary/empty/symlink all excluded
    assert!(lang(&v, "JavaScript").is_none());
    let total_files = v["summary"]["files"].as_u64().unwrap();
    assert_eq!(total_files, 4, "python×2 + rust + makefile: {v}");
    // counting semantics: main.py = 3 loc, 2 sloc
    let py = lang(&v, "Python").unwrap();
    assert_eq!(py["loc"], 5); // main.py 3 + script 2
    assert_eq!(py["sloc"], 4);
    assert_eq!(py["lines"], py["sloc"], "aggregate lines aliases sloc");
}

#[test]
fn cache_roundtrip() {
    let tmp = tempfile::tempdir().unwrap();
    make_tree(tmp.path());

    // cold run writes the cache
    let cold = run_json(tmp.path(), &[]);
    let cache_path = tmp.path().join(".symbol/linguist.msgpack.zst");
    assert!(cache_path.exists());

    // warm run: all hits, zero misses, identical output
    let out = bin()
        .arg(tmp.path())
        .arg("--json")
        .arg("-v")
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&out.stderr);
    let warm: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(cold["summary"], warm["summary"]);
    assert_eq!(cold["languages"], warm["languages"]);
    let misses = stderr
        .lines()
        .find(|l| l.contains("cache"))
        .and_then(|l| l.split(" hits / ").nth(1))
        .and_then(|s| s.split(' ').next())
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or_else(|| panic!("no cache line in stderr: {stderr}"));
    assert_eq!(misses, 0, "warm run must be all hits: {stderr}");

    // touch one file with new mtime AND size → re-classifies that file plus
    // link.py, whose followed-metadata mtime tracks the same target
    std::thread::sleep(std::time::Duration::from_millis(20));
    std::fs::write(
        tmp.path().join("src/main.py"),
        "import os\n\nprint(1)\n# more\n",
    )
    .unwrap();
    let out = bin()
        .arg(tmp.path())
        .arg("--json")
        .arg("-v")
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&out.stderr);
    let expected_misses = if cfg!(unix) { "2 misses" } else { "1 misses" };
    assert!(
        stderr.contains(expected_misses),
        "expected {expected_misses}: {stderr}"
    );
    let touched: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(lang(&touched, "Python").unwrap()["loc"], 6);

    // corrupt cache → clean cold start, correct output
    std::fs::write(&cache_path, b"garbage not zstd").unwrap();
    let recovered = run_json(tmp.path(), &[]);
    assert_eq!(recovered["languages"], touched["languages"]);
}

#[test]
fn no_heuristics_flag_changes_chain() {
    let tmp = tempfile::tempdir().unwrap();
    let objc = "#import <Foundation/Foundation.h>\n@interface F : NSObject\n@end\n";
    std::fs::write(tmp.path().join("thing.h"), objc).unwrap();

    let with = run_json(tmp.path(), &["--no-cache"]);
    let without = run_json(tmp.path(), &["--no-cache", "--no-heuristics"]);
    assert!(lang(&with, "Objective-C").is_some(), "{with}");
    assert!(
        lang(&without, "C").is_some(),
        "_PREFERRED .h → C: {without}"
    );
}
