use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::process::Command;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::git::{clone_at, load_manifest, repo_path};
use crate::schema::{
    ComposedCase, Corpus, DecisionKind, ExerciseStatus, ExercisedCase, RecordedCall, TaskSpec,
    VerificationStatus, VerifiedCase,
};
use crate::verify::{child_file, symbol, worktree_changed_files};

const READ_OPS: &[&str] = &["search", "code", "outline", "callers", "stats", "loc"];
const WRITE_OPS: &[&str] = &[
    "patch",
    "multi-patch",
    "delete-symbol",
    "insert-symbol",
    "rename-symbol",
    "replace-symbol",
];
const MAX_ACTOR_TURNS: usize = 12;
const MAX_READ_CALLS: usize = 6;

#[derive(Debug, Deserialize, Serialize)]
struct ActorDecision {
    action: String,
    #[serde(default)]
    args: Vec<String>,
    reason: String,
}

#[allow(clippy::too_many_arguments)]
pub fn run(
    manifest_path: &Path,
    input: &Path,
    repo_cache: &Path,
    symbol_bin: &Path,
    grok_bin: &Path,
    model: &str,
    out: &Path,
    verified_input: Option<&Path>,
    skip: usize,
    limit: Option<usize>,
    keep_failures: bool,
) -> Result<()> {
    let manifest = load_manifest(manifest_path)?;
    let corpora: HashMap<&str, &Corpus> = manifest
        .corpora
        .iter()
        .map(|corpus| (corpus.id.as_str(), corpus))
        .collect();
    let verified_ids = verified_input.map(load_verified_ids).transpose()?;
    if let Some(parent) = out.parent() {
        fs::create_dir_all(parent)?;
    }
    let reader = BufReader::new(fs::File::open(input)?);
    let mut writer = BufWriter::new(fs::File::create(out)?);
    let mut processed = 0usize;
    let mut exact = 0usize;
    let mut eligible = 0usize;
    for (line_number, line) in reader.lines().enumerate() {
        if limit.is_some_and(|limit| processed >= limit) {
            break;
        }
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let composed: ComposedCase = serde_json::from_str(&line)
            .with_context(|| format!("parse composed line {}", line_number + 1))?;
        if !matches!(composed.composition.decision, DecisionKind::Accept) {
            continue;
        }
        if verified_ids
            .as_ref()
            .is_some_and(|ids| !ids.contains(&composed.candidate.id))
        {
            continue;
        }
        let Some(task) = composed.composition.task.clone() else {
            continue;
        };
        if eligible < skip {
            eligible += 1;
            continue;
        }
        eligible += 1;
        processed += 1;
        let corpus = corpora
            .get(composed.candidate.corpus.as_str())
            .with_context(|| format!("unknown corpus {}", composed.candidate.corpus))?;
        let record = exercise_one(
            &composed,
            task,
            corpus,
            repo_cache,
            symbol_bin,
            grok_bin,
            model,
            keep_failures,
        );
        if matches!(record.status, ExerciseStatus::VerifiedExact) {
            exact += 1;
        }
        println!(
            "{:?} {} ({exact}/{processed} exact)",
            record.status, record.candidate.id
        );
        for error in &record.errors {
            println!("  {error}");
        }
        serde_json::to_writer(&mut writer, &record)?;
        writer.write_all(b"\n")?;
    }
    writer.flush()?;
    println!("Grok completed {processed} tasks; {exact} matched exactly");
    Ok(())
}

fn load_verified_ids(path: &Path) -> Result<HashSet<String>> {
    BufReader::new(fs::File::open(path)?)
        .lines()
        .enumerate()
        .try_fold(HashSet::new(), |mut ids, (index, line)| {
            let line = line?;
            if !line.trim().is_empty() {
                let record: VerifiedCase = serde_json::from_str(&line)
                    .with_context(|| format!("parse verified line {}", index + 1))?;
                if matches!(record.status, VerificationStatus::VerifiedExact) {
                    ids.insert(record.candidate_id);
                }
            }
            Ok(ids)
        })
}

#[allow(clippy::too_many_arguments)]
fn exercise_one(
    composed: &ComposedCase,
    task: TaskSpec,
    corpus: &Corpus,
    repo_cache: &Path,
    symbol_bin: &Path,
    grok_bin: &Path,
    model: &str,
    keep_failures: bool,
) -> ExercisedCase {
    let temp = match tempfile::Builder::new()
        .prefix(&format!("symbol-exercise-{}-", corpus.id))
        .tempdir()
    {
        Ok(temp) => temp,
        Err(error) => return error_record(composed, task, model, error.into()),
    };
    let project = temp.path().join("project");
    let calls_path = temp.path().join("symbol-calls.jsonl");
    let result = exercise_inner(
        composed,
        &task,
        corpus,
        repo_cache,
        symbol_bin,
        grok_bin,
        model,
        &project,
        &calls_path,
    );
    let mut record = match result {
        Ok(record) => record,
        Err(error) => error_record(composed, task, model, error),
    };
    if keep_failures && !matches!(record.status, ExerciseStatus::VerifiedExact) {
        let kept = temp.keep();
        record.project_root = Some(kept.join("project").display().to_string());
    }
    record
}

#[allow(clippy::too_many_arguments)]
fn exercise_inner(
    composed: &ComposedCase,
    task: &TaskSpec,
    corpus: &Corpus,
    repo_cache: &Path,
    symbol_bin: &Path,
    grok_bin: &Path,
    model: &str,
    project: &Path,
    calls_path: &Path,
) -> Result<ExercisedCase> {
    let repo = repo_path(repo_cache, corpus);
    clone_at(&repo, &composed.candidate.parent, project)?;
    let indexed = symbol(symbol_bin, project, &["index".into()])?;
    if !indexed.status.success() {
        bail!("index failed: {}", String::from_utf8_lossy(&indexed.stderr));
    }
    let transcript = run_actor(grok_bin, model, project, symbol_bin, calls_path, task)?;
    let calls = read_calls(calls_path)?;
    let mut errors = audit_trace(&calls, task);
    let changed_files = worktree_changed_files(project)?;
    let mut expected_files = task.expected_files.clone();
    expected_files.sort();
    let mut actual_files = changed_files.clone();
    actual_files.sort();
    if actual_files != expected_files {
        errors.push(format!(
            "changed files differ: expected {expected_files:?}, got {actual_files:?}"
        ));
    }
    for rel in &task.expected_files {
        match child_file(&repo, &composed.candidate.commit, rel)? {
            Some(expected) => match fs::read(project.join(rel)) {
                Ok(actual) if actual == expected => {}
                Ok(_) => errors.push(format!("{rel} does not exactly match child commit")),
                Err(error) => errors.push(format!("cannot read resulting {rel}: {error}")),
            },
            None if !project.join(rel).exists() => {}
            None => errors.push(format!("{rel} should be deleted in child commit")),
        }
    }
    let undo_verified = if matches!(task.primary_op.as_str(), "rename-symbol" | "replace-symbol") {
        let undo = symbol(symbol_bin, project, &["undo".into()])?;
        let clean = undo.status.success() && worktree_changed_files(project)?.is_empty();
        if !clean {
            errors.push("undo did not restore the exact parent tree".into());
        }
        clean
    } else {
        false
    };
    Ok(ExercisedCase {
        version: 1,
        status: if errors.is_empty() {
            ExerciseStatus::VerifiedExact
        } else {
            ExerciseStatus::Rejected
        },
        model: model.into(),
        candidate: composed.candidate.clone(),
        task: task.clone(),
        calls,
        changed_files,
        undo_verified,
        transcript,
        project_root: None,
        errors,
    })
}

fn run_actor(
    grok_bin: &Path,
    model: &str,
    project: &Path,
    symbol_bin: &Path,
    calls_path: &Path,
    task: &TaskSpec,
) -> Result<String> {
    let mut transcript = String::new();
    for _ in 0..MAX_ACTOR_TURNS {
        let calls = read_calls(calls_path)?;
        let prompt = actor_prompt(task, &calls)?;
        let decision = ask_actor(grok_bin, model, &prompt)?;
        transcript.push_str(&serde_json::to_string(&decision)?);
        transcript.push('\n');
        match decision.action.as_str() {
            "finish" => break,
            "call" => {
                validate_actor_args(&decision.args)?;
                let op = decision.args[0].as_str();
                let is_write = WRITE_OPS.contains(&op);
                let read_count = calls
                    .iter()
                    .filter_map(|call| recorded_op(&call.args))
                    .filter(|op| READ_OPS.contains(op))
                    .count();
                if !is_write && read_count >= MAX_READ_CALLS {
                    bail!("Grok actor exceeded the {MAX_READ_CALLS}-call read budget");
                }
                let mut args = vec![
                    "--root".into(),
                    project.display().to_string(),
                    "--json".into(),
                ];
                args.extend(decision.args);
                let exit_code = proxy(symbol_bin, calls_path, &args)?;
                if is_write && exit_code == 0 {
                    break;
                }
            }
            other => bail!("Grok actor returned unknown action {other:?}"),
        }
    }
    Ok(transcript)
}

fn ask_actor(grok_bin: &Path, model: &str, prompt: &str) -> Result<ActorDecision> {
    let temp = tempfile::tempdir()?;
    let prompt_path = temp.path().join("actor-prompt.txt");
    fs::write(&prompt_path, prompt)?;
    let schema = serde_json::to_string(&actor_schema())?;
    let mut last_error = None;
    for _ in 0..3 {
        let output = Command::new(grok_bin)
            .arg("--model")
            .arg(model)
            .arg("--prompt-file")
            .arg(&prompt_path)
            .arg("--json-schema")
            .arg(&schema)
            .args([
                "--output-format",
                "json",
                "--max-turns",
                "5",
                "--no-subagents",
                "--disable-web-search",
                "--no-memory",
                "--no-plan",
                "--verbatim",
            ])
            .output()
            .context("run Grok task actor decision")?;
        if !output.status.success() {
            last_error = Some(format!(
                "Grok actor decision failed ({}): {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
            continue;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        match parse_actor_output(&stdout) {
            Ok(decision) => return Ok(decision),
            Err(error) => last_error = Some(format!("raw Grok actor output: {stdout}\n{error:#}")),
        }
    }
    bail!(
        "Grok actor produced no valid decision after 3 attempts: {}",
        last_error.unwrap_or_else(|| "unknown error".into())
    )
}

fn actor_prompt(task: &TaskSpec, calls: &[RecordedCall]) -> Result<String> {
    let reads_used = calls
        .iter()
        .filter_map(|call| recorded_op(&call.args))
        .filter(|op| READ_OPS.contains(op))
        .count();
    Ok(format!(
        r#"You are the decision component in a Rust-controlled symbol-rs loop.
Do not invoke tools yourself. Your entire response is structured controller data selecting one argv call for Rust to execute, or finish.

Complete this real refactoring task using only mediated symbol-rs calls:

{title}
{objective}

Relevant source files: {expected_files}
Suggested initial symbol reads: {pre_reads}

STRICT TOOL CONTRACT
- The project is already indexed.
- Select exactly one symbol-rs call for Rust to execute by returning action="call" and its argv tokens, or action="finish".
- You have no shell, filesystem, native read/edit, web, or other tools.
- Discover code with search, code, outline, callers, stats, or loc before changing it.
- You have a strict budget of {max_reads} read calls; {reads_used} have already been used. When the budget is exhausted, your next call must be the write.
- search finds symbol declarations by one name/pattern. Do not combine alternatives with `|`; use code on a discovered qualified path or the suggested file range.
- Make exactly the requested change using one write operation.
- args must be argv tokens beginning with the operation, without symbol-rs, --root, or --json.
- Use outputs from prior calls below. Never invent output.
- Do not return finish until the requested write has succeeded.

Read syntax:
  ["search", PATTERN, "--fixed"?, "--kind", KIND?, "--file", FILE?]
  ["code", QUALIFIED_PATH]
  ["code", "FILE:START-END"]
  ["outline", FILE_OR_QUALIFIED_PATH]
  ["callers", NAME]
  ["stats"]
  ["loc"]

Write syntax:
  ["rename-symbol", QUALIFIED_PATH, NEW_NAME]
  ["replace-symbol", QUALIFIED_PATH, COMPLETE_DEFINITION]
  ["insert-symbol", ANCHOR, "before"|"after"|"start"|"end", CONTENT]
  ["delete-symbol", QUALIFIED_PATH, "--force"?]
  ["patch", FILE, "START-END", CONTENT, "--force"?]
  ["multi-patch", FILE, EDITS_JSON, "--force"?]
    where EDITS_JSON is a JSON array of non-overlapping {{"range":"START-END","content":"..."}} or {{"old":"exact text","content":"..."}} objects for that file

Prior mediated symbol calls and results:
{calls}
"#,
        title = task.title,
        objective = task.objective,
        expected_files = serde_json::to_string(&task.expected_files)?,
        pre_reads = serde_json::to_string(&task.pre_reads)?,
        max_reads = MAX_READ_CALLS,
        reads_used = reads_used,
        calls = serde_json::to_string_pretty(&actor_history(calls))?,
    ))
}

fn actor_history(calls: &[RecordedCall]) -> Vec<Value> {
    calls
        .iter()
        .map(|call| {
            let argv = call
                .args
                .iter()
                .position(|arg| arg == "--json")
                .map_or(call.args.as_slice(), |index| &call.args[index + 1..]);
            serde_json::json!({
                "sequence": call.sequence,
                "args": argv,
                "exit_code": call.exit_code,
                "result": call.json.as_ref().map_or_else(
                    || Value::String(format!("{}{}", call.stdout, call.stderr)),
                    Clone::clone,
                )
            })
        })
        .collect()
}

fn actor_schema() -> Value {
    serde_json::json!({
        "type": "object",
        "additionalProperties": false,
        "required": ["action", "args", "reason"],
        "properties": {
            "action": {"type": "string", "enum": ["call", "finish"]},
            "args": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"}
        }
    })
}

fn parse_actor_output(output: &str) -> Result<ActorDecision> {
    let value: Value =
        serde_json::from_str(output.trim()).context("Grok actor output is not JSON")?;
    find_actor_decision(&value).context("Grok JSON envelope contains no actor decision")
}

fn find_actor_decision(value: &Value) -> Option<ActorDecision> {
    if value.get("action").is_some() {
        if let Ok(decision) = serde_json::from_value(value.clone()) {
            return Some(decision);
        }
    }
    match value {
        Value::Object(map) => map.values().find_map(|child| {
            find_actor_decision(child).or_else(|| {
                child
                    .as_str()
                    .and_then(|text| serde_json::from_str::<Value>(text).ok())
                    .and_then(|nested| find_actor_decision(&nested))
            })
        }),
        Value::Array(values) => values.iter().find_map(find_actor_decision),
        _ => None,
    }
}

fn validate_actor_args(args: &[String]) -> Result<()> {
    let op = args.first().context("Grok actor returned an empty call")?;
    if !READ_OPS.contains(&op.as_str()) && !WRITE_OPS.contains(&op.as_str()) {
        bail!("Grok actor requested unsupported operation {op:?}");
    }
    if args
        .iter()
        .skip(1)
        .any(|arg| arg == "--root" || arg == "--json")
    {
        bail!("Grok actor attempted to pass forbidden global arguments");
    }
    Ok(())
}

fn read_calls(path: &Path) -> Result<Vec<RecordedCall>> {
    if !path.is_file() {
        return Ok(vec![]);
    }
    BufReader::new(fs::File::open(path)?)
        .lines()
        .enumerate()
        .filter_map(|(index, line)| match line {
            Ok(line) if !line.trim().is_empty() => Some(
                serde_json::from_str(&line)
                    .with_context(|| format!("parse recorded call line {}", index + 1)),
            ),
            Ok(_) => None,
            Err(error) => Some(Err(error.into())),
        })
        .collect()
}

fn audit_trace(calls: &[RecordedCall], task: &TaskSpec) -> Vec<String> {
    let mut errors = Vec::new();
    if calls.is_empty() {
        errors.push("Grok made no recorded symbol calls".into());
    }
    let mut primary_count = 0usize;
    for call in calls {
        if call.exit_code != 0 {
            errors.push(format!(
                "recorded symbol call {} exited {}",
                call.sequence, call.exit_code
            ));
        }
        let Some(op) = recorded_op(&call.args) else {
            errors.push(format!("cannot identify operation in call {:?}", call.args));
            continue;
        };
        if op == task.primary_op {
            primary_count += 1;
        } else if WRITE_OPS.contains(&op) {
            errors.push(format!("unexpected write operation {op:?}"));
        } else if !READ_OPS.contains(&op) && op != "index" {
            errors.push(format!("unsupported recorded operation {op:?}"));
        }
    }
    if primary_count != 1 {
        errors.push(format!(
            "expected exactly one {:?} call, got {primary_count}",
            task.primary_op
        ));
    }
    errors
}

fn recorded_op(args: &[String]) -> Option<&str> {
    let json = args.iter().position(|arg| arg == "--json")?;
    args.get(json + 1).map(String::as_str)
}

fn error_record(
    composed: &ComposedCase,
    task: TaskSpec,
    model: &str,
    error: anyhow::Error,
) -> ExercisedCase {
    ExercisedCase {
        version: 1,
        status: ExerciseStatus::Error,
        model: model.into(),
        candidate: composed.candidate.clone(),
        task,
        calls: vec![],
        changed_files: vec![],
        undo_verified: false,
        transcript: String::new(),
        project_root: None,
        errors: vec![format!("{error:#}")],
    }
}

pub fn proxy(actual: &Path, log: &Path, args: &[String]) -> Result<i32> {
    let output = Command::new(actual)
        .args(args)
        .output()
        .with_context(|| format!("proxy symbol call {args:?}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    let sequence = if log.is_file() {
        BufReader::new(fs::File::open(log)?)
            .lines()
            .filter(|line| line.as_ref().is_ok_and(|line| !line.trim().is_empty()))
            .count()
            + 1
    } else {
        1
    };
    let call = RecordedCall {
        sequence,
        args: args.to_vec(),
        exit_code: output.status.code().unwrap_or(-1),
        json: serde_json::from_str(&stdout).ok(),
        stdout: stdout.clone(),
        stderr: stderr.clone(),
    };
    if let Some(parent) = log.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = fs::OpenOptions::new().create(true).append(true).open(log)?;
    serde_json::to_writer(&mut file, &call)?;
    file.write_all(b"\n")?;
    print!("{stdout}");
    eprint!("{stderr}");
    Ok(call.exit_code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_requires_one_primary_write() {
        let task = TaskSpec {
            title: "Rename".into(),
            objective: "Rename a symbol".into(),
            primary_op: "rename-symbol".into(),
            args: Default::default(),
            pre_reads: vec![],
            expected_files: vec!["a.py".into()],
            coverage_tags: vec![],
            confidence: 1.0,
        };
        let call = RecordedCall {
            sequence: 1,
            args: vec![
                "--root".into(),
                "/tmp/x".into(),
                "--json".into(),
                "rename-symbol".into(),
            ],
            exit_code: 0,
            json: None,
            stdout: String::new(),
            stderr: String::new(),
        };
        assert!(audit_trace(&[call], &task).is_empty());
        assert!(!audit_trace(&[], &task).is_empty());
    }

    #[test]
    fn actor_calls_are_limited_to_symbol_operations() {
        assert!(validate_actor_args(&["code".into(), "pkg.mod.fn".into()]).is_ok());
        assert!(validate_actor_args(&["patch".into(), "a.py".into()]).is_ok());
        assert!(validate_actor_args(&["cat".into(), "a.py".into()]).is_err());
        assert!(validate_actor_args(&["code".into(), "--root".into()]).is_err());
    }
}
