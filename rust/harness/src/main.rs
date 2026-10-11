use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

use anyhow::{bail, Context, Result};
use clap::Parser;
use serde::{Deserialize, Serialize};
use serde_json::Value;

const SUPPORTED_OPS: &[&str] = &[
    "index",
    "refresh",
    "search",
    "code",
    "outline",
    "callers",
    "patch",
    "multi-patch",
    "delete-symbol",
    "insert-symbol",
    "rename-symbol",
    "replace-symbol",
    "undo",
    "stats",
    "loc",
];
const HARNESS_OPS: &[&str] = &["append-file", "write-file"];

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// JSON scenario configuration
    config: PathBuf,
    /// symbol binary to exercise
    #[arg(long)]
    bin: Option<PathBuf>,
    /// Bare corpus mirror directory for external scenarios
    #[arg(long)]
    repo_cache: Option<PathBuf>,
    /// Run only the named scenario (repeatable)
    #[arg(long)]
    scenario: Vec<String>,
    /// Write the complete machine-readable report
    #[arg(long)]
    report: Option<PathBuf>,
    /// Keep every temporary project (failed projects are always kept)
    #[arg(long)]
    keep: bool,
}

#[derive(Deserialize)]
struct Config {
    version: u64,
    fixture: Option<String>,
    corpora_manifest: Option<String>,
    repo_cache: Option<String>,
    scenarios: Vec<Scenario>,
}

#[derive(Deserialize)]
struct Scenario {
    id: String,
    corpus: Option<String>,
    parent: Option<String>,
    #[serde(default = "default_true")]
    auto_index: bool,
    #[serde(default)]
    fail_fast: bool,
    #[serde(default)]
    steps: Vec<Step>,
}

#[derive(Deserialize)]
struct Step {
    id: String,
    op: String,
    #[serde(default = "empty_object")]
    args: Value,
    #[serde(default)]
    argv: Option<Vec<String>>,
    read: Option<String>,
    #[serde(default)]
    prepare: Vec<Preparation>,
    #[serde(default)]
    expect: Expect,
    capture_tree: Option<String>,
}

#[derive(Deserialize)]
struct Preparation {
    op: String,
    #[serde(default = "empty_object")]
    args: Value,
}

#[derive(Deserialize, Default)]
struct Expect {
    exit_code: Option<i32>,
    #[serde(default)]
    json: BTreeMap<String, Value>,
    #[serde(default)]
    json_contains: Vec<String>,
    #[serde(default)]
    files: BTreeMap<String, FileExpect>,
    tree_changed: Option<bool>,
    tree_equals: Option<String>,
    golden_commit: Option<String>,
    #[serde(default)]
    golden_files: Vec<String>,
}

#[derive(Deserialize)]
struct ExternalCorpusManifest {
    corpora: Vec<ExternalCorpus>,
}

#[derive(Deserialize)]
struct ExternalCorpus {
    id: String,
}

struct SourceContext {
    fixture: Option<PathBuf>,
    corpus_repos: HashMap<String, PathBuf>,
}

#[derive(Deserialize)]
struct FileExpect {
    #[serde(default)]
    contains: Vec<String>,
    #[serde(default)]
    not_contains: Vec<String>,
    equals: Option<String>,
}

fn default_true() -> bool {
    true
}

fn empty_object() -> Value {
    serde_json::json!({})
}

#[derive(Serialize, Clone)]
struct Invocation {
    command: Vec<String>,
    exit_code: i32,
    duration_ms: f64,
    json: Option<Value>,
    stdout: String,
    stderr: String,
    parse_error: Option<String>,
}

#[derive(Serialize)]
struct StepReport {
    id: String,
    op: String,
    passed: bool,
    errors: Vec<String>,
    preparations: Vec<Invocation>,
    tree_changed: bool,
    #[serde(flatten)]
    invocation: Invocation,
}

#[derive(Serialize)]
struct ScenarioReport {
    id: String,
    passed: bool,
    root: String,
    steps: Vec<StepReport>,
}

#[derive(Serialize)]
struct Summary {
    scenarios: usize,
    passed_scenarios: usize,
    steps: usize,
    passed_steps: usize,
    duration_ms: f64,
}

#[derive(Serialize)]
struct Report {
    config: String,
    binary: String,
    summary: Summary,
    scenarios: Vec<ScenarioReport>,
}

type Tree = BTreeMap<String, String>;

fn main() -> Result<()> {
    let cli = Cli::parse();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .context("resolve repository root")?;
    let config_path = cli.config.canonicalize().context("resolve config path")?;
    let config: Config = serde_json::from_slice(&fs::read(&config_path)?)
        .with_context(|| format!("parse {}", config_path.display()))?;
    let sources = validate_config(&config, &config_path, cli.repo_cache.as_deref(), &repo)?;
    let binary = cli
        .bin
        .unwrap_or_else(|| repo.join("rust/target/debug/symbol"))
        .canonicalize()
        .context("resolve symbol binary; build it with `cargo build -p symbol-core`")?;

    let selected: HashSet<&str> = cli.scenario.iter().map(String::as_str).collect();
    let available: HashSet<&str> = config.scenarios.iter().map(|s| s.id.as_str()).collect();
    let missing: Vec<&&str> = selected.difference(&available).collect();
    if !missing.is_empty() {
        bail!(
            "unknown scenarios: {}",
            missing.iter().map(|s| **s).collect::<Vec<_>>().join(", ")
        );
    }

    let started = Instant::now();
    let mut scenarios = Vec::new();
    for scenario in &config.scenarios {
        if selected.is_empty() || selected.contains(scenario.id.as_str()) {
            scenarios.push(execute_scenario(
                scenario, &sources, &binary, &repo, cli.keep,
            )?);
        }
    }
    let steps = scenarios.iter().map(|s| s.steps.len()).sum();
    let passed_steps = scenarios
        .iter()
        .flat_map(|s| &s.steps)
        .filter(|s| s.passed)
        .count();
    let summary = Summary {
        scenarios: scenarios.len(),
        passed_scenarios: scenarios.iter().filter(|s| s.passed).count(),
        steps,
        passed_steps,
        duration_ms: elapsed_ms(started),
    };
    println!(
        "\n{}/{} steps passed in {:.2}s",
        summary.passed_steps,
        summary.steps,
        summary.duration_ms / 1000.0
    );

    let report = Report {
        config: config_path.display().to_string(),
        binary: binary.display().to_string(),
        summary,
        scenarios,
    };
    if let Some(path) = cli.report {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(
            path,
            format!("{}\n", serde_json::to_string_pretty(&report)?),
        )?;
    }
    if report.summary.passed_steps != report.summary.steps {
        std::process::exit(1);
    }
    Ok(())
}

fn validate_config(
    config: &Config,
    config_path: &Path,
    repo_cache_override: Option<&Path>,
    repo: &Path,
) -> Result<SourceContext> {
    if config.version != 1 {
        bail!("config version must be 1");
    }
    let base = config_path.parent().unwrap_or_else(|| Path::new("."));
    let fixture = config
        .fixture
        .as_ref()
        .map(|fixture| base.join(fixture).canonicalize())
        .transpose()
        .context("resolve fixture directory")?;
    let mut corpus_repos = HashMap::new();
    if let Some(manifest_rel) = &config.corpora_manifest {
        let manifest_path = base.join(manifest_rel).canonicalize()?;
        let manifest: ExternalCorpusManifest = serde_json::from_slice(&fs::read(&manifest_path)?)?;
        let cache = repo_cache_override
            .map(Path::to_owned)
            .or_else(|| config.repo_cache.as_ref().map(|path| base.join(path)))
            .unwrap_or_else(|| repo.join(".tmp/corpora"));
        for corpus in manifest.corpora {
            corpus_repos.insert(corpus.id.clone(), cache.join(format!("{}.git", corpus.id)));
        }
    }
    let mut scenario_ids = HashSet::new();
    for scenario in &config.scenarios {
        if scenario.id.is_empty() || !scenario_ids.insert(&scenario.id) {
            bail!("scenario id is empty or duplicated: {:?}", scenario.id);
        }
        if let Some(corpus) = &scenario.corpus {
            if scenario.parent.is_none() {
                bail!("external scenario {} has no parent commit", scenario.id);
            }
            let mirror = corpus_repos.get(corpus).with_context(|| {
                format!("scenario {} uses unknown corpus {corpus}", scenario.id)
            })?;
            if !mirror.is_dir() {
                bail!("corpus mirror is missing: {}", mirror.display());
            }
        } else if fixture.is_none() {
            bail!(
                "scenario {} has neither a corpus nor a fixture",
                scenario.id
            );
        }
        let mut step_ids = HashSet::new();
        for step in &scenario.steps {
            if step.id.is_empty() || !step_ids.insert(&step.id) {
                bail!(
                    "step id is empty or duplicated in {}: {:?}",
                    scenario.id,
                    step.id
                );
            }
            if !SUPPORTED_OPS.contains(&step.op.as_str())
                && !HARNESS_OPS.contains(&step.op.as_str())
            {
                bail!(
                    "unsupported operation in {}/{}: {:?}",
                    scenario.id,
                    step.id,
                    step.op
                );
            }
            for prep in &step.prepare {
                if !SUPPORTED_OPS.contains(&prep.op.as_str()) {
                    bail!(
                        "unsupported preparation in {}/{}: {:?}",
                        scenario.id,
                        step.id,
                        prep.op
                    );
                }
            }
        }
    }
    Ok(SourceContext {
        fixture,
        corpus_repos,
    })
}

fn execute_scenario(
    scenario: &Scenario,
    sources: &SourceContext,
    binary: &Path,
    repo: &Path,
    keep: bool,
) -> Result<ScenarioReport> {
    let temp = tempfile::Builder::new()
        .prefix(&format!("symbol-{}-", scenario.id))
        .tempdir()?;
    let root = temp.path().join("project");
    let golden_repo = if let Some(corpus) = &scenario.corpus {
        let mirror = sources.corpus_repos.get(corpus).unwrap();
        clone_external(mirror, scenario.parent.as_deref().unwrap(), &root)?;
        Some(mirror.as_path())
    } else {
        copy_dir(sources.fixture.as_deref().unwrap(), &root)?;
        None
    };
    let mut snapshots = HashMap::from([("fixture".to_owned(), tree_hash(&root)?)]);
    let session = format!("feature-harness-{}", scenario.id);
    let mut steps = Vec::new();

    if scenario.auto_index {
        let indexed = run_cli(
            binary,
            &root,
            "index",
            &empty_object(),
            None,
            repo,
            &session,
        )?;
        if indexed.exit_code != 0 || indexed.parse_error.is_some() {
            steps.push(StepReport {
                id: "<auto-index>".into(),
                op: "index".into(),
                passed: false,
                errors: vec!["automatic index failed".into()],
                preparations: vec![],
                tree_changed: false,
                invocation: indexed,
            });
        } else {
            snapshots.insert("initial".into(), tree_hash(&root)?);
        }
    }

    if steps.is_empty() {
        for step in &scenario.steps {
            let mut preparations = Vec::new();
            if let Some(target) = &step.read {
                preparations.push(run_cli(
                    binary,
                    &root,
                    "code",
                    &serde_json::json!({"target": target}),
                    None,
                    repo,
                    &session,
                )?);
            }
            for prep in &step.prepare {
                preparations.push(run_cli(
                    binary, &root, &prep.op, &prep.args, None, repo, &session,
                )?);
            }

            let before = tree_hash(&root)?;
            let invocation = if HARNESS_OPS.contains(&step.op.as_str()) {
                run_harness_op(&root, &step.op, &step.args)?
            } else {
                run_cli(
                    binary,
                    &root,
                    &step.op,
                    &step.args,
                    step.argv.as_deref(),
                    repo,
                    &session,
                )?
            };
            let after = tree_hash(&root)?;
            let mut errors = Vec::new();
            for (i, prep) in preparations.iter().enumerate() {
                if prep.exit_code != 0 || prep.parse_error.is_some() {
                    errors.push(format!("preparation {} failed", i + 1));
                }
            }
            errors.extend(assert_result(
                &invocation,
                &step.expect,
                &root,
                &before,
                &after,
                &snapshots,
                golden_repo,
            )?);
            if let Some(name) = &step.capture_tree {
                snapshots.insert(name.clone(), after.clone());
            }
            let passed = errors.is_empty();
            println!(
                "{:<4}  {}/{}  {}  {:.2} ms",
                if passed { "PASS" } else { "FAIL" },
                scenario.id,
                step.id,
                step.op,
                invocation.duration_ms
            );
            for error in &errors {
                println!("      {error}");
            }
            steps.push(StepReport {
                id: step.id.clone(),
                op: step.op.clone(),
                passed,
                errors,
                preparations,
                tree_changed: before != after,
                invocation,
            });
            if !passed && scenario.fail_fast {
                break;
            }
        }
    }

    let passed = steps.iter().all(|s| s.passed);
    let root_display = if keep || !passed {
        temp.keep().display().to_string() + "/project"
    } else {
        "<removed>".into()
    };
    Ok(ScenarioReport {
        id: scenario.id.clone(),
        passed,
        root: root_display,
        steps,
    })
}

fn run_cli(
    binary: &Path,
    root: &Path,
    op: &str,
    args: &Value,
    raw_argv: Option<&[String]>,
    repo: &Path,
    session: &str,
) -> Result<Invocation> {
    let mut command = vec![
        binary.display().to_string(),
        "--root".into(),
        root.display().to_string(),
        "--json".into(),
    ];
    if let Some(argv) = raw_argv {
        command.push(op.to_owned());
        command.extend(argv.iter().cloned());
    } else {
        command.extend(cli_args(op, args)?);
    }
    let started = Instant::now();
    let output = Command::new(binary)
        .args(&command[1..])
        .current_dir(repo)
        .env("SYMBOL_SESSION_ID", session)
        .env(
            "UV_CACHE_DIR",
            std::env::var_os("UV_CACHE_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| repo.join(".tmp/uv-cache")),
        )
        .output()
        .with_context(|| format!("run {op}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    let parsed = serde_json::from_str::<Value>(&stdout);
    let (json, parse_error) = match parsed {
        Ok(value) => (Some(value), None),
        Err(error) => (None, Some(error.to_string())),
    };
    Ok(Invocation {
        command,
        exit_code: output.status.code().unwrap_or(-1),
        duration_ms: elapsed_ms(started),
        json,
        stdout,
        stderr,
        parse_error,
    })
}

fn run_harness_op(root: &Path, op: &str, args: &Value) -> Result<Invocation> {
    let file = string_arg(args, "file")?;
    let path = root.join(file);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let content = optional_string(args, "content").unwrap_or_default();
    if op == "append-file" {
        use std::io::Write;
        fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)?
            .write_all(content.as_bytes())?;
    } else {
        fs::write(&path, content)?;
    }
    Ok(Invocation {
        command: vec![format!("<{op}>"), file.into()],
        exit_code: 0,
        duration_ms: 0.0,
        json: Some(serde_json::json!({"status": "applied", "file": file})),
        stdout: String::new(),
        stderr: String::new(),
        parse_error: None,
    })
}

fn cli_args(op: &str, args: &Value) -> Result<Vec<String>> {
    let mut out = vec![op.to_owned()];
    match op {
        "index" | "refresh" | "undo" | "stats" => {}
        "search" => {
            if let Some(patterns) = args.get("patterns").and_then(Value::as_array) {
                out.extend(patterns.iter().filter_map(Value::as_str).map(str::to_owned));
            }
            for key in ["kind", "file", "limit"] {
                if let Some(value) = args.get(key).filter(|v| !v.is_null()) {
                    out.push(format!("--{}", key.replace('_', "-")));
                    out.push(value_to_arg(value));
                }
            }
            for key in ["regex", "fixed", "ignore_case"] {
                if bool_arg(args, key) {
                    out.push(format!("--{}", key.replace('_', "-")));
                }
            }
        }
        "code" => out.push(string_arg(args, "target")?.into()),
        "outline" => out.push(string_arg(args, "query")?.into()),
        "callers" => out.push(string_arg(args, "name")?.into()),
        "patch" => {
            out.extend([
                string_arg(args, "file")?.into(),
                string_arg(args, "range")?.into(),
                optional_string(args, "content").unwrap_or_default(),
            ]);
            write_flags(&mut out, args, true);
        }
        "multi-patch" => {
            out.extend([
                string_arg(args, "file")?.into(),
                serde_json::to_string(
                    args.get("edits")
                        .and_then(Value::as_array)
                        .context("missing array argument \"edits\"")?,
                )?,
            ]);
            write_flags(&mut out, args, true);
        }
        "delete-symbol" => {
            out.push(string_arg(args, "qualified_path")?.into());
            write_flags(&mut out, args, true);
        }
        "insert-symbol" => {
            out.extend([
                string_arg(args, "anchor_path")?.into(),
                string_arg(args, "position")?.into(),
                optional_string(args, "content").unwrap_or_default(),
            ]);
            if bool_arg(args, "no_reindent") {
                out.push("--no-reindent".into());
            }
            write_flags(&mut out, args, false);
        }
        "rename-symbol" => {
            out.extend([
                string_arg(args, "qualified_path")?.into(),
                string_arg(args, "new_name")?.into(),
            ]);
            write_flags(&mut out, args, false);
        }
        "replace-symbol" => {
            out.extend([
                string_arg(args, "qualified_path")?.into(),
                string_arg(args, "content")?.into(),
            ]);
            write_flags(&mut out, args, false);
        }
        "loc" => {
            if let Some(path) = optional_string(args, "path") {
                out.push(path);
            }
            for key in [
                "files",
                "classify",
                "no_vendor_skip",
                "no_heuristics",
                "no_cache",
                "verbose",
            ] {
                if bool_arg(args, key) {
                    out.push(format!("--{}", key.replace('_', "-")));
                }
            }
            if let Some(values) = args.get("exclude").and_then(Value::as_array) {
                for value in values.iter().filter_map(Value::as_str) {
                    out.extend(["--exclude".into(), value.into()]);
                }
            }
            if let Some(path) = optional_string(args, "config_dir") {
                out.extend(["--config-dir".into(), path]);
            }
        }
        _ => bail!("unsupported operation: {op}"),
    }
    Ok(out)
}

fn write_flags(out: &mut Vec<String>, args: &Value, allow_force: bool) {
    if allow_force && bool_arg(args, "force") {
        out.push("--force".into());
    }
    if bool_arg(args, "dry_run") {
        out.push("--dry-run".into());
    }
}

fn assert_result(
    result: &Invocation,
    expect: &Expect,
    root: &Path,
    before: &Tree,
    after: &Tree,
    snapshots: &HashMap<String, Tree>,
    golden_repo: Option<&Path>,
) -> Result<Vec<String>> {
    let mut errors = Vec::new();
    let expected_exit = expect.exit_code.unwrap_or(0);
    if result.exit_code != expected_exit {
        errors.push(format!(
            "exit_code: expected {expected_exit}, got {}",
            result.exit_code
        ));
    }
    if let Some(error) = &result.parse_error {
        errors.push(format!("stdout is not JSON: {error}"));
    }
    if let Some(payload) = &result.json {
        for (selector, wanted) in &expect.json {
            match select(payload, selector) {
                Ok(actual) if &actual == wanted => {}
                Ok(actual) => {
                    errors.push(format!("json {selector}: expected {wanted}, got {actual}"))
                }
                Err(error) => errors.push(format!("json {selector}: {error}")),
            }
        }
        let rendered = serde_json::to_string(payload)?;
        for needle in &expect.json_contains {
            if !rendered.contains(needle) {
                errors.push(format!("JSON does not contain {needle:?}"));
            }
        }
    }
    for (rel, checks) in &expect.files {
        let path = root.join(rel);
        let Ok(text) = fs::read_to_string(&path) else {
            errors.push(format!("expected file does not exist: {rel}"));
            continue;
        };
        for needle in &checks.contains {
            if !text.contains(needle) {
                errors.push(format!("{rel} does not contain {needle:?}"));
            }
        }
        for needle in &checks.not_contains {
            if text.contains(needle) {
                errors.push(format!("{rel} unexpectedly contains {needle:?}"));
            }
        }
        if let Some(wanted) = &checks.equals {
            if &text != wanted {
                errors.push(format!("{rel} does not equal configured content"));
            }
        }
    }
    if let Some(wanted) = expect.tree_changed {
        if (before != after) != wanted {
            errors.push(format!(
                "tree_changed: expected {wanted}, got {}",
                before != after
            ));
        }
    }
    if let Some(name) = &expect.tree_equals {
        match snapshots.get(name) {
            Some(tree) if tree == after => {}
            Some(_) => errors.push(format!("source tree does not match snapshot {name:?}")),
            None => errors.push(format!("unknown tree snapshot {name:?}")),
        }
    }
    if let Some(commit) = &expect.golden_commit {
        let Some(git_dir) = golden_repo else {
            errors.push("golden_commit requires an external corpus scenario".into());
            return Ok(errors);
        };
        for rel in &expect.golden_files {
            match git_file(git_dir, commit, rel)? {
                Some(expected) => match fs::read(root.join(rel)) {
                    Ok(actual) if actual == expected => {}
                    Ok(_) => errors.push(format!("{rel} does not match golden commit {commit}")),
                    Err(error) => errors.push(format!("cannot read {rel}: {error}")),
                },
                None if !root.join(rel).exists() => {}
                None => errors.push(format!("{rel} should be absent at golden commit {commit}")),
            }
        }
    }
    Ok(errors)
}

fn select(value: &Value, selector: &str) -> Result<Value> {
    if selector.is_empty() || selector == "$" {
        return Ok(value.clone());
    }
    let mut current = value;
    for part in selector.split('.') {
        if part == "length" {
            let len = match current {
                Value::Array(values) => values.len(),
                Value::Object(values) => values.len(),
                Value::String(value) => value.len(),
                _ => bail!("cannot take length of {current}"),
            };
            return Ok(Value::from(len));
        }
        current = match current {
            Value::Array(values) => values
                .get(
                    part.parse::<usize>()
                        .context("array selector is not an integer")?,
                )
                .with_context(|| format!("array index {part} is out of bounds"))?,
            Value::Object(values) => values
                .get(part)
                .with_context(|| format!("object has no key {part:?}"))?,
            _ => bail!("cannot select {part:?} from {current}"),
        };
    }
    Ok(current.clone())
}

fn copy_dir(source: &Path, target: &Path) -> Result<()> {
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let src = entry.path();
        let dst = target.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir(&src, &dst)?;
        } else {
            fs::copy(&src, &dst)?;
        }
    }
    Ok(())
}

fn clone_external(mirror: &Path, parent: &str, target: &Path) -> Result<()> {
    let clone = Command::new("git")
        .args(["clone", "--shared", "--no-checkout"])
        .arg(mirror)
        .arg(target)
        .output()?;
    if !clone.status.success() {
        bail!(
            "git clone failed: {}",
            String::from_utf8_lossy(&clone.stderr).trim()
        );
    }
    let checkout = Command::new("git")
        .arg("-C")
        .arg(target)
        .args(["checkout", "--detach", parent])
        .output()?;
    if !checkout.status.success() {
        bail!(
            "git checkout {parent} failed: {}",
            String::from_utf8_lossy(&checkout.stderr).trim()
        );
    }
    Ok(())
}

fn git_file(mirror: &Path, commit: &str, rel: &str) -> Result<Option<Vec<u8>>> {
    let output = Command::new("git")
        .arg("--git-dir")
        .arg(mirror)
        .args(["show", &format!("{commit}:{rel}")])
        .output()?;
    Ok(output.status.success().then_some(output.stdout))
}

fn tree_hash(root: &Path) -> Result<Tree> {
    let mut out = BTreeMap::new();
    hash_dir(root, root, &mut out)?;
    Ok(out)
}

fn hash_dir(root: &Path, dir: &Path, out: &mut Tree) -> Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let rel = path
            .strip_prefix(root)?
            .to_string_lossy()
            .replace('\\', "/");
        if rel == ".git"
            || rel.starts_with(".git/")
            || rel == ".symbol"
            || rel.starts_with(".symbol/")
            || rel == ".uv-cache"
            || rel.starts_with(".uv-cache/")
        {
            continue;
        }
        if entry.file_type()?.is_dir() {
            hash_dir(root, &path, out)?;
        } else if entry.file_type()?.is_file() {
            let mut hasher = DefaultHasher::new();
            fs::read(&path)?.hash(&mut hasher);
            out.insert(rel, format!("{:016x}", hasher.finish()));
        }
    }
    Ok(())
}

fn string_arg<'a>(args: &'a Value, key: &str) -> Result<&'a str> {
    args.get(key)
        .and_then(Value::as_str)
        .with_context(|| format!("missing string argument {key:?}"))
}

fn optional_string(args: &Value, key: &str) -> Option<String> {
    args.get(key).and_then(Value::as_str).map(str::to_owned)
}

fn bool_arg(args: &Value, key: &str) -> bool {
    args.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn value_to_arg(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        other => other.to_string(),
    }
}

fn elapsed_ms(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selectors_walk_objects_arrays_and_lengths() {
        let value = serde_json::json!({"items": [{"name": "x"}, {"name": "y"}]});
        assert_eq!(select(&value, "items.1.name").unwrap(), "y");
        assert_eq!(select(&value, "items.length").unwrap(), 2);
    }

    #[test]
    fn multipatch_is_a_supported_operation() {
        assert!(SUPPORTED_OPS.contains(&"multi-patch"));
    }
}
