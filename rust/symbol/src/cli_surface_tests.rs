//! Every command, option, short flag and default of the Python CLI's
//! delegated commands (tests/fixtures/python_cli_surface.json, pinned by
//! tests/test_cli_surface.py) must parse here with the same meaning.

use clap::{CommandFactory, Parser};
use serde_json::Value;

use super::Cli;

const SURFACE: &str = include_str!("../../../tests/fixtures/python_cli_surface.json");

/// Python options this binary does not implement yet.
const NOT_YET: &[(&str, &str)] = &[];

fn surface() -> Value {
    serde_json::from_str(SURFACE).unwrap()
}

fn built() -> clap::Command {
    let mut cmd = Cli::command();
    cmd.build();
    cmd
}

fn find_opt<'a>(cmd: &'a clap::Command, opt: &str) -> Option<&'a clap::Arg> {
    cmd.get_arguments().find(|a| match opt.strip_prefix("--") {
        Some(long) => {
            a.get_long() == Some(long)
                || a.get_all_aliases()
                    .is_some_and(|aliases| aliases.contains(&long))
        }
        None => {
            let short = opt.trim_start_matches('-').chars().next();
            a.get_short() == short
                || a.get_all_short_aliases()
                    .is_some_and(|aliases| short.is_some_and(|s| aliases.contains(&s)))
        }
    })
}

fn sample_value(param: &Value) -> String {
    match (param["name"].as_str(), param["type"].as_str()) {
        (Some("format"), _) => "json".into(),
        (Some("range_"), _) => "1-1".into(),
        (_, Some("integer")) => "3".into(),
        _ => "x".into(),
    }
}

/// Check one Python parameter against the clap command it should live on.
fn check_param(command: &str, cmd: &clap::Command, param: &Value, errors: &mut Vec<String>) {
    let name = param["name"].as_str().unwrap();
    if param["kind"] == "argument" {
        return;
    }
    for opt in param["opts"].as_array().unwrap() {
        let opt = opt.as_str().unwrap();
        if NOT_YET.contains(&(command, opt)) {
            return;
        }
        let Some(arg) = find_opt(cmd, opt) else {
            errors.push(format!("{command}: missing option {opt}"));
            continue;
        };
        let takes_value = arg.get_action().takes_values();
        if param["flag"] == true && takes_value {
            errors.push(format!("{command} {opt}: Python flag takes a value here"));
        }
        if param["flag"] == false && !takes_value {
            errors.push(format!(
                "{command} {opt}: Python option takes a value, flag here"
            ));
        }
        let ours: Vec<String> = arg
            .get_default_values()
            .iter()
            .map(|v| v.to_string_lossy().into_owned())
            .collect();
        let theirs = &param["default"];
        // An unset --format means rich, so no explicit default is needed.
        let implied = name == "format" && *theirs == "rich" && ours.is_empty();
        let expected = match theirs {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        };
        if param["flag"] == false && !implied {
            if let Some(expected) = expected {
                if ours != [expected.clone()] {
                    errors.push(format!(
                        "{command} {opt}: default {ours:?}, Python {expected:?}"
                    ));
                }
            }
        }
    }
}

#[test]
fn every_python_option_exists_with_the_same_shape() {
    let root = built();
    let surface = surface();
    let mut errors = vec![];
    for param in surface["root"].as_array().unwrap() {
        check_param("<root>", &root, param, &mut errors);
    }
    for (command, params) in surface["commands"].as_object().unwrap() {
        let Some(sub) = root.find_subcommand(command) else {
            errors.push(format!("missing command {command}"));
            continue;
        };
        for param in params.as_array().unwrap() {
            check_param(command, sub, param, &mut errors);
        }
        // Positionals line up in order; Python-optional ones stay optional.
        let ours: Vec<&clap::Arg> = sub.get_positionals().collect();
        let theirs: Vec<&Value> = params
            .as_array()
            .unwrap()
            .iter()
            .filter(|p| p["kind"] == "argument")
            .collect();
        for (i, p) in theirs.iter().enumerate() {
            match ours.get(i) {
                None => errors.push(format!("{command}: missing positional {}", p["name"])),
                Some(arg) => {
                    if p["required"] == false && arg.is_required_set() {
                        errors.push(format!(
                            "{command}: positional {} must be optional",
                            p["name"]
                        ));
                    }
                    let many = arg.get_num_args().is_some_and(|n| n.max_values() > 1);
                    if p["multiple"] == true && !many {
                        errors.push(format!(
                            "{command}: positional {} takes one value",
                            p["name"]
                        ));
                    }
                }
            }
        }
    }
    assert!(errors.is_empty(), "{}", errors.join("\n"));
}

#[test]
fn every_python_invocation_parses() {
    let surface = surface();
    let mut failures = vec![];
    for (command, params) in surface["commands"].as_object().unwrap() {
        let params = params.as_array().unwrap();
        let positionals: Vec<String> = params
            .iter()
            .filter(|p| p["kind"] == "argument" && p["required"] == true)
            .map(|_| "x".to_owned())
            .collect();
        let base = || {
            let mut argv = vec!["symbol".to_owned(), command.clone()];
            argv.extend(positionals.iter().cloned());
            argv
        };
        let mut invocations = vec![base()];
        for p in params.iter().filter(|p| p["kind"] == "option") {
            for opt in p["opts"].as_array().unwrap() {
                let opt = opt.as_str().unwrap();
                if NOT_YET.contains(&(command.as_str(), opt)) {
                    continue;
                }
                let mut argv = base();
                argv.push(opt.to_owned());
                if p["flag"] == false {
                    argv.push(sample_value(p));
                }
                invocations.push(argv);
            }
        }
        let mut root_format = vec!["symbol".to_owned(), "--format".into(), "json".into()];
        root_format.extend(base().into_iter().skip(1));
        invocations.push(root_format);
        for argv in invocations {
            if let Err(e) = Cli::try_parse_from(&argv) {
                failures.push(format!("{argv:?}: {}", e.kind()));
            }
        }
    }
    for argv in [vec!["symbol", "--version"], vec!["symbol", "-v", "undo"]] {
        if let Err(e) = Cli::try_parse_from(&argv) {
            failures.push(format!("{argv:?}: {}", e.kind()));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn native_spellings_still_parse() {
    for argv in [
        &["symbol", "--root", ".", "--json", "index"][..],
        &[
            "symbol", "--json", "patch", "f.py", "1-2", "body", "--force",
        ],
        &[
            "symbol",
            "insert-symbol",
            "a.b",
            "after",
            "x = 1",
            "--no-reindent",
        ],
        &["symbol", "replace-symbol", "a.b", "def b(): pass"],
        &["symbol", "multi-patch", "f.py", "[]"],
        &["symbol", "refs", "a.b", "-v"],
        &["symbol", "implementers", "a.B"],
        &["symbol", "map", "-n", "0", "--exclude", "*_test.go"],
        &["symbol", "stats"],
        &["symbol", "loc", "--no-cache", "--files"],
    ] {
        assert!(Cli::try_parse_from(argv).is_ok(), "{argv:?}");
    }
}
