//! `symbol hook` through the compiled binary: PreToolUse nudges and
//! `--enforce` blocks, silent no-ops, and the detached PostToolUse refresh.
//! `golden/hook.json` pins the exact bytes the Python hook printed.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_symbol")
}

fn goscan_built() -> bool {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/wyolet/symbol/adapters/go_ast/daemon/go-scan")
        .is_file()
}

fn big_python() -> String {
    (0..30)
        .map(|i| format!("def step_{i}(x):\n    return x + {i}\n\n\n"))
        .collect()
}

/// An indexed project: a large and a small Python module, a large Go file
/// when go-scan is built, and a large non-code file.
fn project() -> (tempfile::TempDir, PathBuf) {
    project_with(goscan_built())
}

fn project_with(go: bool) -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    std::fs::create_dir_all(root.join("app")).unwrap();
    std::fs::write(root.join("app/big.py"), big_python()).unwrap();
    std::fs::write(root.join("app/small.py"), "def tiny():\n    return 1\n").unwrap();
    std::fs::write(root.join("README.md"), "# notes\n".repeat(120)).unwrap();
    if go {
        std::fs::write(root.join("go.mod"), "module example.com/demo\n\ngo 1.21\n").unwrap();
        let funcs: String = (0..30)
            .map(|i| format!("func Step{i}() int {{\n\treturn {i}\n}}\n\n"))
            .collect();
        std::fs::write(root.join("demo.go"), format!("package demo\n\n{funcs}")).unwrap();
    }
    let out = Command::new(bin())
        .env("SYMBOL_SESSION_ID", "hook-tests")
        .arg("--root")
        .arg(&root)
        .arg("--json")
        .arg("index")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    (dir, root)
}

fn hook(payload: &str, args: &[&str]) -> Output {
    let mut child = Command::new(bin())
        .arg("hook")
        .args(args)
        .env_remove("SYMBOL_AGENT")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(payload.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

fn pre(root: &Path, tool: &str, input: serde_json::Value) -> String {
    serde_json::json!({
        "hook_event_name": "PreToolUse", "cwd": root, "tool_name": tool, "tool_input": input,
    })
    .to_string()
}

fn read_of(root: &Path, rel: &str) -> String {
    pre(
        root,
        "Read",
        serde_json::json!({"file_path": root.join(rel)}),
    )
}

fn context(out: &Output) -> String {
    assert_eq!(out.status.code(), Some(0));
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["hookSpecificOutput"]["permissionDecision"], "allow");
    v["hookSpecificOutput"]["additionalContext"]
        .as_str()
        .unwrap()
        .to_owned()
}

fn silent(out: &Output) {
    assert_eq!(out.status.code(), Some(0));
    assert!(
        out.stdout.is_empty(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
}

#[test]
fn read_nudges_name_the_file_language() {
    let (_dir, root) = project();
    let msg = context(&hook(&read_of(&root, "app/big.py"), &[]));
    assert!(
        msg.starts_with("symbol: \"app/big.py\" is indexed (python)."),
        "{msg}"
    );
    if goscan_built() {
        let msg = context(&hook(&read_of(&root, "demo.go"), &[]));
        assert!(
            msg.starts_with("symbol: \"demo.go\" is indexed (go)."),
            "{msg}"
        );
    }
}

#[test]
fn small_unhandled_and_new_files_pass_silently() {
    let (_dir, root) = project();
    silent(&hook(&read_of(&root, "app/small.py"), &[]));
    silent(&hook(&read_of(&root, "README.md"), &[]));
    let new_file = serde_json::json!({"file_path": root.join("app/new.py")});
    silent(&hook(&pre(&root, "Write", new_file), &[]));
    silent(&hook(&pre(&root, "Bash", serde_json::json!({})), &[]));
    silent(&hook("not json", &[]));
}

#[test]
fn grep_nudges_and_enforce_blocks() {
    let (_dir, root) = project();
    let msg = context(&hook(
        &pre(&root, "Grep", serde_json::json!({"pattern": "step_[0-9]+"})),
        &[],
    ));
    assert!(
        msg.contains("is the name of 5+ indexed declaration(s)"),
        "{msg}"
    );
    let blocked = hook(
        &pre(&root, "Grep", serde_json::json!({"pattern": "step_1"})),
        &["--enforce"],
    );
    assert_eq!(blocked.status.code(), Some(2));
    assert!(blocked.stdout.is_empty());
    assert!(String::from_utf8_lossy(&blocked.stderr).contains("(e.g. app.big.step_1)"));
}

#[test]
fn prints_the_recorded_bytes() {
    let (_dir, root) = project_with(false);
    let golden: serde_json::Value = serde_json::from_str(include_str!("golden/hook.json")).unwrap();
    for case in golden.as_array().unwrap() {
        let mut input = case["tool_input"].clone();
        if let Some(rel) = input["file_path"].as_str() {
            input["file_path"] = serde_json::json!(root.join(rel));
        }
        let args: Vec<&str> = case["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_str().unwrap())
            .collect();
        let out = hook(&pre(&root, case["tool"].as_str().unwrap(), input), &args);
        let root_text = root.to_string_lossy();
        let text = |bytes: &[u8]| String::from_utf8_lossy(bytes).replace(&*root_text, "<root>");
        assert_eq!(
            out.status.code(),
            case["code"].as_i64().map(|c| c as i32),
            "{case}"
        );
        assert_eq!(
            text(&out.stdout),
            case["stdout"].as_str().unwrap(),
            "{case}"
        );
        assert_eq!(
            text(&out.stderr),
            case["stderr"].as_str().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn no_index_means_no_op() {
    let (_dir, root) = project();
    std::fs::remove_file(root.join(".symbol/symbol_index.msgpack.zst")).unwrap();
    silent(&hook(&read_of(&root, "app/big.py"), &[]));
    let elsewhere = tempfile::tempdir().unwrap();
    silent(&hook(
        &pre(
            elsewhere.path(),
            "Grep",
            serde_json::json!({"pattern": "step_1"}),
        ),
        &[],
    ));
}

#[test]
fn post_edit_refreshes_in_the_background() {
    let (_dir, root) = project();
    std::fs::write(
        root.join("app/big.py"),
        big_python() + "def added_by_edit():\n    return 0\n",
    )
    .unwrap();
    let event = serde_json::json!({
        "hook_event_name": "PostToolUse", "cwd": root, "tool_name": "Edit",
        "tool_input": {"file_path": root.join("app/big.py")}, "tool_response": {},
    });
    silent(&hook(&event.to_string(), &[]));
    let probe = pre(
        &root,
        "Grep",
        serde_json::json!({"pattern": "added_by_edit"}),
    );
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let out = hook(&probe, &[]);
        if !out.stdout.is_empty() {
            assert!(context(&out).contains("app.big.added_by_edit"));
            return;
        }
        assert!(Instant::now() < deadline, "index never picked up the edit");
        std::thread::sleep(Duration::from_millis(200));
    }
}
