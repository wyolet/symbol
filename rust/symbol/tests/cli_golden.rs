//! The Python CLI's argv answered with the Python CLI's output: each step's
//! exit code and stdout are pinned in `golden/cli.json`, recorded from the
//! Python implementation on the same fixture. Timestamps are masked.

use std::path::Path;
use std::process::Command;

use regex::Regex;
use serde_json::Value;

const GOLDEN: &str = include_str!("golden/cli.json");

const APP: &str = "import os


class Service:
    \"\"\"Doc — with unicode.\"\"\"

    def save(self, x):
        return os.path.join(x)


def helper(a, b=1):
    s = Service()
    return s.save(a)
";
const CLI: &str = "from app import helper\n\n\ndef main():\n    return helper(1)\n";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_symbol")
}

fn fixture() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let big: String = (0..40)
        .map(|i| format!("def f{i}(x):\n    return x + {i}\n\n\n"))
        .collect();
    std::fs::write(root.join("app.py"), APP).unwrap();
    std::fs::write(root.join("cli.py"), CLI).unwrap();
    std::fs::write(root.join("big.py"), big).unwrap();
    let git = Command::new("git")
        .args(["init", "-q"])
        .current_dir(&root)
        .status()
        .unwrap();
    assert!(git.success());
    (dir, root)
}

fn run(root: &Path, argv: &[String]) -> (i32, String, String) {
    let out = Command::new(bin())
        .args(argv)
        .current_dir(root)
        .env("SYMBOL_SESSION_ID", "parity")
        .env("COLUMNS", "80")
        .env_remove("SYMBOL_AGENT")
        .output()
        .unwrap();
    let mut stdout =
        String::from_utf8_lossy(&out.stdout).replace(&*root.to_string_lossy(), "<root>");
    for (pattern, repl) in [
        (r"\d{13}-(rename|replace)-symbol", "<ts>-$1-symbol"),
        (r"build \d+ ms  •  save \d+ ms", "build <ms>  •  save <ms>"),
    ] {
        stdout = Regex::new(pattern)
            .unwrap()
            .replace_all(&stdout, repl)
            .into_owned();
    }
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    (out.status.code().unwrap_or(-1), stdout, stderr)
}

fn replay(steps: &str) {
    let golden: Value = serde_json::from_str(GOLDEN).unwrap();
    let (_dir, root) = fixture();
    run(&root, &["index".to_owned()]);
    let mut mismatches = vec![];
    for step in golden[steps].as_array().unwrap() {
        let argv: Vec<String> = step["argv"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_str().unwrap().to_owned())
            .collect();
        let (code, stdout, stderr) = run(&root, &argv);
        if code != step["code"] || stdout != step["stdout"] {
            mismatches.push(format!(
                "$ symbol {}\n--- expected {} {}\n--- got {code} {stdout}{stderr}",
                argv.join(" "),
                step["code"],
                step["stdout"].as_str().unwrap(),
            ));
        }
    }
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n\n"));
}

#[test]
fn reads_print_the_recorded_output() {
    replay("reads");
}

#[test]
fn writes_print_the_recorded_output() {
    replay("writes");
}

#[test]
fn root_format_is_rejected_for_commands_without_it() {
    let (_dir, root) = fixture();
    let (code, _, stderr) = run(&root, &["--format".into(), "json".into(), "index".into()]);
    assert_eq!(code, 2);
    // Error panels are colored and wrapped; match the flattened text.
    let plain = Regex::new(r"\x1b\[[0-9;]*m|[│╭╮╰╯─]")
        .unwrap()
        .replace_all(&stderr, " ");
    let flat = plain.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(flat.contains("'index' has no --format option"), "{stderr}");
}
