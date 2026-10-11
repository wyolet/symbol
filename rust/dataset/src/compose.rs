use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::process::Command;

use anyhow::{bail, Context, Result};
use serde_json::Value;

use crate::schema::{Candidate, ComposedCase, ModelDecision};

const PROMPT_VERSION: &str = "single-op-v3-multipatch";

pub struct RunConfig<'a> {
    pub input: &'a Path,
    pub out: &'a Path,
    pub grok_bin: &'a Path,
    pub model: &'a str,
    pub cache_dir: &'a Path,
    pub limit: Option<usize>,
    pub limit_per_corpus: Option<usize>,
    pub require_hint: Option<&'a str>,
}

pub fn run(config: RunConfig<'_>) -> Result<()> {
    fs::create_dir_all(config.cache_dir)?;
    if let Some(parent) = config.out.parent() {
        fs::create_dir_all(parent)?;
    }
    let reader = BufReader::new(fs::File::open(config.input)?);
    let mut writer = BufWriter::new(fs::File::create(config.out)?);
    let mut accepted = 0usize;
    let mut rejected = 0usize;
    let mut selected_by_corpus = HashMap::<String, usize>::new();
    for (index, line) in reader.lines().enumerate() {
        if config.limit.is_some_and(|limit| accepted + rejected >= limit) {
            break;
        }
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let candidate: Candidate = serde_json::from_str(&line)
            .with_context(|| format!("parse candidate line {}", index + 1))?;
        if config.require_hint.is_some_and(|hint| {
            !candidate
                .operation_hints
                .iter()
                .any(|candidate_hint| candidate_hint == hint)
        }) {
            continue;
        }
        let corpus_count = selected_by_corpus
            .entry(candidate.corpus.clone())
            .or_default();
        if config.limit_per_corpus.is_some_and(|limit| *corpus_count >= limit) {
            continue;
        }
        *corpus_count += 1;
        let composition =
            match compose_one(&candidate, config.grok_bin, config.model, config.cache_dir) {
                Ok(composition) => composition,
                Err(error) => {
                    eprintln!("compose {} failed: {error:#}", candidate.id);
                    ModelDecision {
                        decision: crate::schema::DecisionKind::Reject,
                        reason: format!("composer_error: {error}"),
                        task: None,
                    }
                }
            };
        if matches!(composition.decision, crate::schema::DecisionKind::Accept) {
            accepted += 1;
        } else {
            rejected += 1;
        }
        let record = ComposedCase {
            version: 1,
            model: config.model.to_owned(),
            prompt_version: PROMPT_VERSION.into(),
            candidate,
            composition,
        };
        serde_json::to_writer(&mut writer, &record)?;
        writer.write_all(b"\n")?;
        println!(
            "composed {} ({accepted} accepted / {rejected} rejected)",
            record.candidate.id
        );
    }
    writer.flush()?;
    println!(
            "wrote {} composed cases to {}",
            accepted + rejected,
            config.out.display()
        );
    Ok(())
}

fn compose_one(
    candidate: &Candidate,
    grok_bin: &Path,
    model: &str,
    cache_dir: &Path,
) -> Result<ModelDecision> {
    let cache = cache_dir.join(format!(
        "{}-{}-{}-{}.json",
        sanitize(&candidate.corpus),
        &candidate.commit[..candidate.commit.len().min(16)],
        sanitize(model),
        PROMPT_VERSION
    ));
    if cache.is_file() {
        return parse_grok_output(&fs::read_to_string(&cache)?)
            .with_context(|| format!("parse cached Grok response {}", cache.display()));
    }

    let temp = tempfile::tempdir()?;
    let prompt_path = temp.path().join("prompt.txt");
    fs::write(&prompt_path, prompt(candidate)?)?;
    let schema = serde_json::to_string(&decision_schema())?;
    let output = Command::new(grok_bin)
        .arg("--model")
        .arg(model)
        .arg("--prompt-file")
        .arg(&prompt_path)
        .arg("--json-schema")
        .arg(schema)
        .args([
            "--output-format",
            "json",
            "--max-turns",
            "3",
            "--no-subagents",
            "--disable-web-search",
            "--no-memory",
            "--verbatim",
        ])
        .output()
        .context("run Grok composer")?;
    if !output.status.success() {
        bail!(
            "Grok composer failed ({}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    fs::write(&cache, &stdout)?;
    parse_grok_output(&stdout)
}

fn prompt(candidate: &Candidate) -> Result<String> {
    Ok(format!(
        r#"You compose one real symbol-tool task from one real Git commit candidate.

Decide whether ALL source changes shown can be reproduced exactly by ONE supported primary operation:
rename-symbol, replace-symbol, insert-symbol, delete-symbol, patch, or multi-patch.

Rules:
- Reject multi-concern commits, moves, formatting-only changes, generated changes, and changes requiring manual import edits.
- Reject when one supported operation cannot reproduce every byte in expected_files. multi-patch may contain multiple non-overlapping edits but all edits must target one file.
- Never invent a qualified path, file, range, or content. Derive them from the diff and module_context.
- expected_files must contain only candidate files and must be the exact files the operation should change.
- pre_reads are symbol paths or file:A-B reads an agent should perform before the write.
- args must match exactly one of these symbol-rs CLI contracts:
  rename-symbol: {{"qualified_path": string, "new_name": string}}
  replace-symbol: {{"qualified_path": string, "content": string}}
  insert-symbol: {{"anchor_path": string, "position": "before"|"after", "content": string}}
  delete-symbol: {{"qualified_path": string, "force": boolean}}
  patch: {{"file": string, "range": "START-END", "content": string, "force": boolean}}
  multi-patch: {{"file": string, "edits": [{{"range": "START-END", "content": string}} or {{"old": string, "content": string}}], "force": boolean}}
- patch range is inclusive and 1-indexed. replace/insert content is the complete requested content.
- confidence is 0.0 to 1.0. Prefer rejection below 0.85.
- The task title should describe the engineering intent without revealing the exact command.

Candidate JSON:
{}
"#,
        serde_json::to_string_pretty(candidate)?
    ))
}

fn decision_schema() -> Value {
    serde_json::json!({
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": false,
                "required": ["decision", "reason", "task"],
                "properties": {
                    "decision": {"const": "reject"},
                    "reason": {"type": "string"},
                    "task": {"type": "null"}
                }
            },
            {
                "type": "object",
                "additionalProperties": false,
                "required": ["decision", "reason", "task"],
                "properties": {
                    "decision": {"const": "accept"},
                    "reason": {"type": "string"},
                    "task": task_schema()
                }
            }
        ]
    })
}

fn task_schema() -> Value {
    serde_json::json!({
        "type": "object",
        "additionalProperties": false,
        "required": [
            "title", "objective", "primary_op", "args", "pre_reads",
            "expected_files", "coverage_tags", "confidence"
        ],
        "properties": {
            "title": {"type": "string"},
            "objective": {"type": "string"},
            "primary_op": {
                "type": "string",
                "enum": [
                    "rename-symbol", "replace-symbol", "insert-symbol",
                    "delete-symbol", "patch", "multi-patch"
                ]
            },
            "args": {"type": "object"},
            "pre_reads": {"type": "array", "items": {"type": "string"}},
            "expected_files": {
                "type": "array", "minItems": 1, "uniqueItems": true,
                "items": {"type": "string"}
            },
            "coverage_tags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
        }
    })
}

fn parse_grok_output(output: &str) -> Result<ModelDecision> {
    let trimmed = output.trim();
    if let Ok(mut decision) = serde_json::from_str::<ModelDecision>(trimmed) {
        normalize_decision(&mut decision);
        validate_decision(&decision)?;
        return Ok(decision);
    }
    let value: Value = serde_json::from_str(trimmed).context("Grok output is not JSON")?;
    let mut decision =
        find_decision(&value).context("Grok JSON envelope contains no model decision")?;
    normalize_decision(&mut decision);
    validate_decision(&decision)?;
    Ok(decision)
}

fn normalize_decision(decision: &mut ModelDecision) {
    if let Some(task) = &mut decision.task {
        for target in &mut task.pre_reads {
            if let Some(stripped) = target.strip_prefix("file:") {
                *target = stripped.to_owned();
            }
        }
    }
}

fn validate_decision(decision: &ModelDecision) -> Result<()> {
    use crate::schema::DecisionKind;

    match (&decision.decision, &decision.task) {
        (DecisionKind::Accept, None) => bail!("accepted Grok decision has no task"),
        (DecisionKind::Reject, Some(_)) => bail!("rejected Grok decision unexpectedly has a task"),
        (_, None) => Ok(()),
        (_, Some(task)) => {
            crate::verify::task_cli_args(&task.primary_op, &task.args)?;
            if task.expected_files.is_empty() {
                bail!("accepted Grok task has no expected files");
            }
            Ok(())
        }
    }
}

fn find_decision(value: &Value) -> Option<ModelDecision> {
    if value.get("decision").is_some() {
        if let Ok(decision) = serde_json::from_value(value.clone()) {
            return Some(decision);
        }
    }
    match value {
        Value::Object(map) => {
            for child in map.values() {
                if let Some(found) = find_decision(child) {
                    return Some(found);
                }
                if let Some(text) = child.as_str() {
                    if let Ok(nested) = serde_json::from_str::<Value>(strip_fence(text)) {
                        if let Some(found) = find_decision(&nested) {
                            return Some(found);
                        }
                    }
                }
            }
            None
        }
        Value::Array(values) => values.iter().find_map(find_decision),
        _ => None,
    }
}

fn strip_fence(text: &str) -> &str {
    text.trim()
        .strip_prefix("```json")
        .or_else(|| text.trim().strip_prefix("```"))
        .unwrap_or(text.trim())
        .strip_suffix("```")
        .unwrap_or(text.trim())
        .trim()
}

fn sanitize(value: &str) -> String {
    value
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_direct_and_enveloped_decisions() {
        let direct = r#"{"decision":"reject","reason":"too broad","task":null}"#;
        assert!(matches!(
            parse_grok_output(direct).unwrap().decision,
            crate::schema::DecisionKind::Reject
        ));

        let envelope = serde_json::json!({"result": direct}).to_string();
        assert!(matches!(
            parse_grok_output(&envelope).unwrap().decision,
            crate::schema::DecisionKind::Reject
        ));
    }

    #[test]
    fn rejects_accept_without_a_task() {
        let invalid = r#"{"decision":"accept","reason":"missing","task":null}"#;
        assert!(parse_grok_output(invalid).is_err());
    }
}
