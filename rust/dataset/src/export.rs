use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;

use anyhow::{Context, Result};
use serde_json::{Map, Value};

use crate::schema::{ExerciseStatus, ExercisedCase, RecordedCall};

pub fn run(input: &Path, out: &Path, corpora_manifest: &str) -> Result<()> {
    let reader = BufReader::new(fs::File::open(input)?);
    let mut scenarios = Vec::new();
    for (line_number, line) in reader.lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let exercised: ExercisedCase = serde_json::from_str(&line)
            .with_context(|| format!("parse exercised line {}", line_number + 1))?;
        if matches!(exercised.status, ExerciseStatus::VerifiedExact) {
            scenarios.push(scenario(&exercised)?);
        }
    }
    let config = serde_json::json!({
        "version": 1,
        "corpora_manifest": corpora_manifest,
        "scenarios": scenarios,
    });
    if let Some(parent) = out.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(out, format!("{}\n", serde_json::to_string_pretty(&config)?))?;
    println!(
        "exported {} verified scenarios to {}",
        config["scenarios"].as_array().map(Vec::len).unwrap_or(0),
        out.display()
    );
    Ok(())
}

fn scenario(exercised: &ExercisedCase) -> Result<Value> {
    let mut steps = Vec::new();
    for call in &exercised.calls {
        let Some((op, argv)) = call_parts(call) else {
            continue;
        };
        let mut expect = Map::new();
        expect.insert("exit_code".into(), call.exit_code.into());
        let selectors = stable_selectors(op, call.json.as_ref());
        if !selectors.is_empty() {
            expect.insert("json".into(), serde_json::to_value(selectors)?);
        }
        if op == exercised.task.primary_op {
            expect.insert("tree_changed".into(), true.into());
            expect.insert(
                "golden_commit".into(),
                exercised.candidate.commit.clone().into(),
            );
            expect.insert(
                "golden_files".into(),
                serde_json::to_value(&exercised.task.expected_files)?,
            );
        }
        steps.push(serde_json::json!({
            "id": format!("call_{:03}_{}", call.sequence, op.replace('-', "_")),
            "op": op,
            "argv": argv,
            "expect": Value::Object(expect),
        }));
    }
    if exercised.undo_verified {
        steps.push(serde_json::json!({
            "id": "verify_undo",
            "op": "undo",
            "expect": {
                "json": {"status": "undone"},
                "tree_equals": "initial"
            }
        }));
    }
    Ok(serde_json::json!({
        "id": format!(
            "{}_{}",
            exercised.candidate.corpus,
            &exercised.candidate.commit[..exercised.candidate.commit.len().min(12)]
        ),
        "title": exercised.task.title,
        "coverage_tags": exercised.task.coverage_tags,
        "corpus": exercised.candidate.corpus,
        "parent": exercised.candidate.parent,
        "steps": steps,
    }))
}

fn call_parts(call: &RecordedCall) -> Option<(&str, Vec<String>)> {
    let json = call.args.iter().position(|arg| arg == "--json")?;
    let op = call.args.get(json + 1)?.as_str();
    Some((op, call.args[(json + 2)..].to_vec()))
}

fn stable_selectors(op: &str, payload: Option<&Value>) -> BTreeMap<String, Value> {
    let mut out = BTreeMap::new();
    let Some(payload) = payload else { return out };
    if let Some(status) = payload.get("status") {
        out.insert("status".into(), status.clone());
    }
    match op {
        "search" | "outline" | "callers" => {
            if let Some(values) = payload.as_array() {
                out.insert("length".into(), values.len().into());
            }
        }
        "code" => {
            for key in ["path", "file", "kind", "start_line", "end_line"] {
                if let Some(value) = payload.get(key) {
                    out.insert(key.into(), value.clone());
                }
            }
        }
        _ => {}
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_raw_argv_after_global_flags() {
        let call = RecordedCall {
            sequence: 1,
            args: vec![
                "--root".into(),
                "/tmp/p".into(),
                "--json".into(),
                "search".into(),
                "save".into(),
                "--fixed".into(),
            ],
            exit_code: 0,
            json: Some(serde_json::json!([])),
            stdout: String::new(),
            stderr: String::new(),
        };
        let (op, argv) = call_parts(&call).unwrap();
        assert_eq!(op, "search");
        assert_eq!(argv, ["save", "--fixed"]);
    }
}
