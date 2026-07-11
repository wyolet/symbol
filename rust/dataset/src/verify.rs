use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::process::{Command, Output};

use anyhow::{bail, Context, Result};
use serde_json::Value;

use crate::git::{clone_at, load_manifest, repo_path};
use crate::schema::{ComposedCase, Corpus, DecisionKind, VerificationStatus, VerifiedCase};

pub fn run(
    manifest_path: &Path,
    input: &Path,
    repo_cache: &Path,
    symbol_bin: &Path,
    out: &Path,
    run_checks: bool,
    limit: Option<usize>,
) -> Result<()> {
    let manifest = load_manifest(manifest_path)?;
    let corpora: HashMap<&str, &Corpus> = manifest
        .corpora
        .iter()
        .map(|corpus| (corpus.id.as_str(), corpus))
        .collect();
    if let Some(parent) = out.parent() {
        fs::create_dir_all(parent)?;
    }
    let reader = BufReader::new(fs::File::open(input)?);
    let mut writer = BufWriter::new(fs::File::create(out)?);
    let mut processed = 0usize;
    let mut verified = 0usize;
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
        processed += 1;
        let corpus = corpora
            .get(composed.candidate.corpus.as_str())
            .with_context(|| format!("unknown corpus {}", composed.candidate.corpus))?;
        let record = verify_one(&composed, corpus, repo_cache, symbol_bin, run_checks);
        if matches!(record.status, VerificationStatus::VerifiedExact) {
            verified += 1;
        }
        println!(
            "{:?} {} ({verified}/{processed} exact)",
            record.status, record.candidate_id
        );
        serde_json::to_writer(&mut writer, &record)?;
        writer.write_all(b"\n")?;
    }
    writer.flush()?;
    println!("verified {verified}/{processed} accepted tasks exactly");
    Ok(())
}

fn verify_one(
    composed: &ComposedCase,
    corpus: &Corpus,
    repo_cache: &Path,
    symbol_bin: &Path,
    run_checks: bool,
) -> VerifiedCase {
    match verify_inner(composed, corpus, repo_cache, symbol_bin, run_checks) {
        Ok(mut record) => {
            record.status = if record.errors.is_empty() {
                VerificationStatus::VerifiedExact
            } else {
                VerificationStatus::Rejected
            };
            record
        }
        Err(error) => base_record(
            composed,
            VerificationStatus::Error,
            vec![format!("{error:#}")],
        ),
    }
}

fn verify_inner(
    composed: &ComposedCase,
    corpus: &Corpus,
    repo_cache: &Path,
    symbol_bin: &Path,
    run_checks: bool,
) -> Result<VerifiedCase> {
    let task = composed
        .composition
        .task
        .as_ref()
        .context("accepted composition has no task")?;
    let repo = repo_path(repo_cache, corpus);
    let temp = tempfile::tempdir()?;
    let project = temp.path().join("project");
    clone_at(&repo, &composed.candidate.parent, &project)?;
    let mut errors = Vec::new();

    let indexed = symbol(symbol_bin, &project, &["index".into()])?;
    if !indexed.status.success() {
        bail!("index failed: {}", String::from_utf8_lossy(&indexed.stderr));
    }
    for target in &task.pre_reads {
        let output = symbol(symbol_bin, &project, &["code".into(), target.clone()])?;
        if !output.status.success() {
            errors.push(format!("pre-read failed for {target:?}"));
        }
    }
    let op_args = task_cli_args(&task.primary_op, &task.args)?;
    let operation = symbol(symbol_bin, &project, &op_args)?;
    if !operation.status.success() {
        errors.push(format!(
            "operation exited {}: {}{}",
            operation.status,
            String::from_utf8_lossy(&operation.stdout),
            String::from_utf8_lossy(&operation.stderr)
        ));
    } else if serde_json::from_slice::<Value>(&operation.stdout)
        .ok()
        .and_then(|value| {
            value
                .get("status")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .as_deref()
        != Some("applied")
    {
        errors.push("operation did not return status=applied".into());
    }

    let changed_files = worktree_changed_files(&project)?;
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

    let check_passed = if run_checks && !corpus.check_command.is_empty() {
        let passed = Command::new(&corpus.check_command[0])
            .args(&corpus.check_command[1..])
            .current_dir(&project)
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
        if !passed {
            errors.push("corpus check command failed".into());
        }
        Some(passed)
    } else {
        None
    };

    let undo_verified = if matches!(task.primary_op.as_str(), "rename-symbol" | "replace-symbol") {
        let undo = symbol(symbol_bin, &project, &["undo".into()])?;
        let clean = undo.status.success() && worktree_changed_files(&project)?.is_empty();
        if !clean {
            errors.push("undo did not restore the exact parent tree".into());
        }
        clean
    } else {
        false
    };

    Ok(VerifiedCase {
        version: 1,
        status: VerificationStatus::Rejected,
        candidate_id: composed.candidate.id.clone(),
        corpus: composed.candidate.corpus.clone(),
        parent: composed.candidate.parent.clone(),
        commit: composed.candidate.commit.clone(),
        model: composed.model.clone(),
        task: Some(task.clone()),
        changed_files,
        undo_verified,
        check_passed,
        errors,
    })
}

fn base_record(
    composed: &ComposedCase,
    status: VerificationStatus,
    errors: Vec<String>,
) -> VerifiedCase {
    VerifiedCase {
        version: 1,
        status,
        candidate_id: composed.candidate.id.clone(),
        corpus: composed.candidate.corpus.clone(),
        parent: composed.candidate.parent.clone(),
        commit: composed.candidate.commit.clone(),
        model: composed.model.clone(),
        task: composed.composition.task.clone(),
        changed_files: vec![],
        undo_verified: false,
        check_passed: None,
        errors,
    }
}

pub(crate) fn symbol(binary: &Path, project: &Path, args: &[String]) -> Result<Output> {
    Command::new(binary)
        .arg("--root")
        .arg(project)
        .arg("--json")
        .args(args)
        .env("SYMBOL_SESSION_ID", "dataset-verifier")
        .output()
        .with_context(|| format!("run symbol-rs {args:?}"))
}

pub(crate) fn task_cli_args(
    op: &str,
    args: &std::collections::BTreeMap<String, Value>,
) -> Result<Vec<String>> {
    let string = |key: &str| -> Result<String> {
        args.get(key)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .with_context(|| format!("missing string argument {key:?}"))
    };
    let mut out = vec![op.to_owned()];
    match op {
        "rename-symbol" => out.extend([string("qualified_path")?, string("new_name")?]),
        "replace-symbol" => out.extend([string("qualified_path")?, string("content")?]),
        "insert-symbol" => out.extend([
            string("anchor_path")?,
            string("position")?,
            string("content")?,
        ]),
        "delete-symbol" => {
            out.push(string("qualified_path")?);
            if args.get("force").and_then(Value::as_bool) == Some(true) {
                out.push("--force".into());
            }
        }
        "patch" => {
            out.extend([string("file")?, string("range")?, string("content")?]);
            if args.get("force").and_then(Value::as_bool) == Some(true) {
                out.push("--force".into());
            }
        }
        "multi-patch" => {
            out.extend([
                string("file")?,
                serde_json::to_string(
                    args.get("edits")
                        .and_then(Value::as_array)
                        .context("missing array argument \"edits\"")?,
                )?,
            ]);
            if args.get("force").and_then(Value::as_bool) == Some(true) {
                out.push("--force".into());
            }
        }
        _ => bail!("unsupported primary operation {op:?}"),
    }
    Ok(out)
}

pub(crate) fn worktree_changed_files(project: &Path) -> Result<Vec<String>> {
    let output = Command::new("git")
        .arg("-C")
        .arg(project)
        .args(["diff", "--name-only"])
        .output()?;
    if !output.status.success() {
        bail!("git diff failed");
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| !line.is_empty())
        .map(str::to_owned)
        .collect())
}

pub(crate) fn child_file(repo: &Path, commit: &str, rel: &str) -> Result<Option<Vec<u8>>> {
    let output = Command::new("git")
        .arg("--git-dir")
        .arg(repo)
        .args(["show", &format!("{commit}:{rel}")])
        .output()?;
    if output.status.success() {
        Ok(Some(output.stdout))
    } else {
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_supported_task_arguments() {
        let args = std::collections::BTreeMap::from([
            ("qualified_path".into(), Value::String("pkg.old".into())),
            ("new_name".into(), Value::String("new".into())),
        ]);
        assert_eq!(
            task_cli_args("rename-symbol", &args).unwrap(),
            ["rename-symbol", "pkg.old", "new"]
        );
    }
}
