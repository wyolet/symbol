//! Integration tests for `symbol-rs insert-symbol` (WP6). Each test drives the
//! compiled binary against a throwaway project, so the real pyscan daemon runs
//! (index build + first_decorator_line). Placement and indentation are checked
//! by reading the file back after the splice.

use std::path::Path;
use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_symbol-rs")
}

/// A fresh project with a decorated function, a plain function, and a class
/// with a method — enough to exercise all four positions.
fn setup() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let app = dir.path().join("app");
    std::fs::create_dir_all(&app).unwrap();
    std::fs::write(
        app.join("mod.py"),
        "import functools\n\
         \n\
         \n\
         @functools.cache\n\
         def cached_thing(x):\n\
         \x20\x20\x20\x20return x + 1\n\
         \n\
         \n\
         def plain(y):\n\
         \x20\x20\x20\x20return y\n\
         \n\
         \n\
         class Svc:\n\
         \x20\x20\x20\x20def save(self, u):\n\
         \x20\x20\x20\x20\x20\x20\x20\x20return u\n",
    )
    .unwrap();
    run(dir.path(), &["index"]);
    dir
}

fn run(root: &Path, args: &[&str]) -> serde_json::Value {
    let out = Command::new(bin())
        .arg("--root")
        .arg(root)
        .arg("--json")
        .args(args)
        .output()
        .expect("spawn symbol-rs");
    let stdout = String::from_utf8_lossy(&out.stdout);
    // `index` prints a stats line, not necessarily parseable as one value we
    // care about; only the write ops are parsed by callers.
    serde_json::from_str(&stdout).unwrap_or(serde_json::Value::Null)
}

fn read(root: &Path, rel: &str) -> String {
    std::fs::read_to_string(root.join(rel)).unwrap()
}

#[test]
fn after_lands_below_anchor() {
    let dir = setup();
    let root = dir.path();
    let res = run(
        root,
        &["insert-symbol", "app.mod.plain", "after", "def helper():\n    return 1"],
    );
    assert_eq!(res["status"], "applied", "{res}");
    let text = read(root, "app/mod.py");
    // helper is a new top-level def after plain, no leading indent.
    assert!(text.contains("\ndef helper():\n    return 1\n"), "{text}");
    let idx_plain = text.find("def plain").unwrap();
    let idx_helper = text.find("def helper").unwrap();
    let idx_svc = text.find("class Svc").unwrap();
    // Lands immediately below plain, above the following class.
    assert!(idx_plain < idx_helper && idx_helper < idx_svc, "{text}");
}

#[test]
fn before_lands_above_decorator() {
    let dir = setup();
    let root = dir.path();
    let res = run(
        root,
        &["insert-symbol", "app.mod.cached_thing", "before", "def top():\n    return 0"],
    );
    assert_eq!(res["status"], "applied", "{res}");
    let text = read(root, "app/mod.py");
    let idx_top = text.find("def top").unwrap();
    let idx_deco = text.find("@functools.cache").unwrap();
    // The insert must land ABOVE the decorator, not between it and the def.
    assert!(idx_top < idx_deco, "top should precede the decorator:\n{text}");
}

#[test]
fn start_indents_into_method_body() {
    let dir = setup();
    let root = dir.path();
    let res = run(
        root,
        &["insert-symbol", "app.mod.Svc.save", "start", "self.audit()"],
    );
    assert_eq!(res["status"], "applied", "{res}");
    let text = read(root, "app/mod.py");
    // save is at 4-space indent; body is 8 spaces. start lands right after the
    // signature at body indent (8 spaces).
    assert!(text.contains("\n        self.audit()\n"), "{text}");
    let sig = text.find("def save").unwrap();
    let audit = text.find("self.audit()").unwrap();
    let ret = text.find("return u").unwrap();
    assert!(sig < audit && audit < ret, "{text}");
}

#[test]
fn end_indents_into_class_body() {
    let dir = setup();
    let root = dir.path();
    let res = run(
        root,
        &["insert-symbol", "app.mod.Svc", "end", "def extra(self):\n    return 9"],
    );
    assert_eq!(res["status"], "applied", "{res}");
    let text = read(root, "app/mod.py");
    // end of class Svc: content lands at body indent (4 spaces).
    assert!(text.contains("\n    def extra(self):\n        return 9\n"), "{text}");
}

#[test]
fn dry_run_leaves_tree_untouched() {
    let dir = setup();
    let root = dir.path();
    let before = read(root, "app/mod.py");
    let res = run(
        root,
        &["insert-symbol", "app.mod.plain", "after", "def helper():\n    return 1", "--dry-run"],
    );
    assert_eq!(res["status"], "dry_run", "{res}");
    let after = read(root, "app/mod.py");
    assert_eq!(before, after, "dry-run must not write");
}

#[test]
fn no_reindent_sends_content_verbatim() {
    let dir = setup();
    let root = dir.path();
    // With --no-reindent the content is spliced as-is (only the patch engine's
    // trailing-newline fixup applies), so the odd indent survives.
    let res = run(
        root,
        &["insert-symbol", "app.mod.plain", "after", "  raw = 1", "--no-reindent"],
    );
    assert_eq!(res["status"], "applied", "{res}");
    let text = read(root, "app/mod.py");
    assert!(text.contains("\n  raw = 1\n"), "{text}");
}

#[test]
fn invalid_position_is_refused() {
    let dir = setup();
    let root = dir.path();
    let res = run(
        root,
        &["insert-symbol", "app.mod.plain", "middle", "x = 1"],
    );
    assert_eq!(res["status"], "error", "{res}");
    assert_eq!(res["error_code"], "invalid_argument", "{res}");
}

#[test]
fn unknown_anchor_is_refused() {
    let dir = setup();
    let root = dir.path();
    let res = run(
        root,
        &["insert-symbol", "app.mod.nope", "after", "x = 1"],
    );
    assert_eq!(res["status"], "error", "{res}");
    assert_eq!(res["error_code"], "symbol_not_found", "{res}");
}
