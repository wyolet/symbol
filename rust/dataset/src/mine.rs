use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::Path;
use std::sync::LazyLock;

use anyhow::{Context, Result};
use regex::Regex;

use crate::git::{git_output, load_manifest, repo_path};
use crate::schema::{Candidate, Corpus, DeclarationDelta, FileDelta};

static PY_DECL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:async\s+)?(?:(def)\s+([A-Za-z_]\w*)|(class)\s+([A-Za-z_]\w*))").unwrap()
});
static PY_CONST: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^([A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=").unwrap());
static GO_METHOD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w*)\s*\(").unwrap());
static GO_TYPE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(type|var|const)\s+([A-Za-z_]\w*)\b").unwrap());
static NOISE_SUMMARY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)(?:^|\b)(release|version\s+\d|bump|dependabot|changelog|docs?(?:\b|:)|godoc|comment|typo|format(?:ter|ting)?|gofmt|ruff|lint|type[ -]?hint)(?:\b|:)",
    )
    .unwrap()
});

pub fn run(
    manifest_path: &Path,
    repo_cache: &Path,
    out_path: &Path,
    limit_per_corpus: Option<usize>,
) -> Result<()> {
    let manifest = load_manifest(manifest_path)?;
    if let Some(parent) = out_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(fs::File::create(out_path)?);
    let mut total = 0usize;
    for corpus in &manifest.corpora {
        let repo = repo_path(repo_cache, corpus);
        let limit = limit_per_corpus.unwrap_or(usize::MAX);
        let candidates = mine_corpus(corpus, &repo, limit)
            .with_context(|| format!("mine corpus {}", corpus.id))?;
        println!("{}: {} candidates", corpus.id, candidates.len());
        for candidate in candidates {
            serde_json::to_writer(&mut writer, &candidate)?;
            writer.write_all(b"\n")?;
            total += 1;
        }
    }
    writer.flush()?;
    println!("wrote {total} candidates to {}", out_path.display());
    Ok(())
}

fn mine_corpus(corpus: &Corpus, repo: &Path, limit: usize) -> Result<Vec<Candidate>> {
    let revision = corpus.revision.as_deref().unwrap_or("HEAD");
    let mut log_args = vec![
        "log".into(),
        "--no-merges".into(),
        format!("-n{}", corpus.history_limit),
        "--format=%H%x09%P%x09%s".into(),
        revision.into(),
    ];
    if !corpus.source_paths.is_empty() {
        log_args.push("--".into());
        log_args.extend(corpus.source_paths.clone());
    }
    let log = git_output(repo, &log_args)?;
    let mut out = Vec::new();
    for line in log.lines() {
        if out.len() >= limit {
            break;
        }
        let mut fields = line.splitn(3, '\t');
        let Some(commit) = fields.next() else {
            continue;
        };
        let Some(parents) = fields.next() else {
            continue;
        };
        let summary = fields.next().unwrap_or_default();
        let mut parent_fields = parents.split_whitespace();
        let Some(parent) = parent_fields.next() else {
            continue;
        };
        if parent_fields.next().is_some() {
            continue;
        }
        let Some(candidate) = mine_commit(corpus, repo, parent, commit, summary)? else {
            continue;
        };
        out.push(candidate);
    }
    Ok(out)
}

fn mine_commit(
    corpus: &Corpus,
    repo: &Path,
    parent: &str,
    commit: &str,
    summary: &str,
) -> Result<Option<Candidate>> {
    if NOISE_SUMMARY.is_match(summary) {
        return Ok(None);
    }
    let mut numstat_args = vec![
        "diff".into(),
        "--numstat".into(),
        parent.into(),
        commit.into(),
    ];
    if !corpus.source_paths.is_empty() {
        numstat_args.push("--".into());
        numstat_args.extend(corpus.source_paths.clone());
    }
    let numstat = git_output(repo, &numstat_args)?;
    let mut files = Vec::new();
    for line in numstat.lines() {
        let mut fields = line.splitn(3, '\t');
        let (Some(added), Some(deleted), Some(path)) =
            (fields.next(), fields.next(), fields.next())
        else {
            continue;
        };
        if added == "-" || deleted == "-" || !source_file(path, &corpus.language) {
            continue;
        }
        if corpus
            .exclude_paths
            .iter()
            .any(|prefix| path.starts_with(prefix))
        {
            continue;
        }
        files.push(FileDelta {
            path: path.to_owned(),
            added: added.parse().unwrap_or(0),
            deleted: deleted.parse().unwrap_or(0),
        });
    }
    if files.is_empty() || files.len() > corpus.max_files {
        return Ok(None);
    }
    let added_lines = files.iter().map(|f| f.added).sum();
    let deleted_lines = files.iter().map(|f| f.deleted).sum();
    if added_lines + deleted_lines == 0 || added_lines + deleted_lines > corpus.max_diff_lines {
        return Ok(None);
    }

    let mut diff_args = vec![
        "diff".into(),
        "--no-ext-diff".into(),
        "--unified=4".into(),
        parent.into(),
        commit.into(),
        "--".into(),
    ];
    diff_args.extend(files.iter().map(|f| f.path.clone()));
    let diff = git_output(repo, &diff_args)?;
    if diff.len() > 200_000 {
        return Ok(None);
    }
    let declarations = declaration_deltas(&diff, &corpus.language);
    let operation_hints = operation_hints(
        &declarations,
        added_lines,
        deleted_lines,
        files.len(),
        &diff,
    );
    let module_context = module_context(corpus, repo, parent, &files);
    Ok(Some(Candidate {
        version: 1,
        id: format!("{}:{}", corpus.id, commit),
        corpus: corpus.id.clone(),
        language: corpus.language.clone(),
        parent: parent.to_owned(),
        commit: commit.to_owned(),
        summary: summary.to_owned(),
        files,
        module_context,
        added_lines,
        deleted_lines,
        diff,
        declarations,
        operation_hints,
    }))
}

fn module_context(
    corpus: &Corpus,
    repo: &Path,
    parent: &str,
    files: &[FileDelta],
) -> BTreeMap<String, String> {
    let go_module = if corpus.language == "go" {
        git_output(repo, &["show".into(), format!("{parent}:go.mod")])
            .ok()
            .and_then(|text| {
                text.lines()
                    .map(str::trim)
                    .find_map(|line| line.strip_prefix("module ").map(str::trim))
                    .map(str::to_owned)
            })
    } else {
        None
    };
    files
        .iter()
        .map(|file| {
            let path = Path::new(&file.path);
            let prefix = if corpus.language == "python" {
                let mut parts: Vec<String> = path
                    .components()
                    .map(|part| part.as_os_str().to_string_lossy().into_owned())
                    .collect();
                if parts.first().map(String::as_str) == Some("src") {
                    parts.remove(0);
                }
                if let Some(last) = parts.last_mut() {
                    *last = last.trim_end_matches(".py").to_owned();
                }
                if parts.last().map(String::as_str) == Some("__init__") {
                    parts.pop();
                }
                parts.join(".")
            } else {
                let dir = path
                    .parent()
                    .map(|p| p.to_string_lossy().replace('\\', "/"))
                    .unwrap_or_default();
                match (&go_module, dir.is_empty()) {
                    (Some(module), true) => module.clone(),
                    (Some(module), false) => format!("{module}/{dir}"),
                    (None, _) => dir,
                }
            };
            (file.path.clone(), prefix)
        })
        .collect()
}

fn source_file(path: &str, language: &str) -> bool {
    match language {
        "python" => path.ends_with(".py"),
        "go" => path.ends_with(".go") && !path.ends_with("_test.go"),
        _ => false,
    }
}

fn declaration_deltas(diff: &str, language: &str) -> Vec<DeclarationDelta> {
    let mut out = Vec::new();
    for raw in diff.lines() {
        if raw.starts_with("+++") || raw.starts_with("---") {
            continue;
        }
        let Some(sign) = raw.chars().next().filter(|c| *c == '+' || *c == '-') else {
            continue;
        };
        let line = raw[1..].trim_start();
        let parsed = if language == "python" {
            parse_python_decl(line)
        } else {
            parse_go_decl(line)
        };
        if let Some((kind, name)) = parsed {
            out.push(DeclarationDelta {
                sign: sign.to_string(),
                kind,
                name,
                line: line.to_owned(),
            });
        }
    }
    out
}

fn parse_python_decl(line: &str) -> Option<(String, String)> {
    if let Some(caps) = PY_DECL.captures(line) {
        if let Some(name) = caps.get(2) {
            return Some(("function".into(), name.as_str().into()));
        }
        if let Some(name) = caps.get(4) {
            return Some(("class".into(), name.as_str().into()));
        }
    }
    PY_CONST
        .captures(line)
        .map(|caps| ("constant".into(), caps[1].into()))
}

fn parse_go_decl(line: &str) -> Option<(String, String)> {
    if let Some(caps) = GO_METHOD.captures(line) {
        return Some(("function_or_method".into(), caps[1].into()));
    }
    GO_TYPE
        .captures(line)
        .map(|caps| (caps[1].into(), caps[2].into()))
}

fn operation_hints(
    declarations: &[DeclarationDelta],
    added_lines: usize,
    deleted_lines: usize,
    file_count: usize,
    diff: &str,
) -> Vec<String> {
    let removed: Vec<_> = declarations.iter().filter(|d| d.sign == "-").collect();
    let added: Vec<_> = declarations.iter().filter(|d| d.sign == "+").collect();
    let mut hints = Vec::new();
    for old in &removed {
        for new in &added {
            if old.kind != new.kind {
                continue;
            }
            if old.name == new.name {
                hints.push("replace-symbol".to_owned());
            } else {
                hints.push("rename-symbol".to_owned());
            }
        }
    }
    if !removed.is_empty() && added.is_empty() {
        hints.push("delete-symbol".into());
    }
    if removed.is_empty() && !added.is_empty() {
        hints.push("insert-symbol".into());
    }
    if added_lines > 0 || deleted_lines > 0 {
        hints.push("patch".into());
    }
    if file_count == 1 && diff.lines().filter(|line| line.starts_with("@@")).count() > 1 {
        hints.push("multi-patch".into());
    }
    let mut seen = HashSet::new();
    hints.retain(|hint| seen.insert(hint.clone()));
    hints
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_python_and_go_declarations() {
        let py = declaration_deltas("-def old(x):\n+def new(x):", "python");
        assert_eq!(py.len(), 2);
        assert_eq!(py[0].name, "old");
        assert_eq!(py[1].name, "new");

        let go = declaration_deltas(
            "-func (c *Client) Send(v string) error {\n+func (c *Client) Dispatch(v string) error {",
            "go",
        );
        assert_eq!(go.len(), 2);
        assert_eq!(go[1].name, "Dispatch");
    }

    #[test]
    fn hints_rename_for_matching_kind_name_change() {
        let decls = vec![
            DeclarationDelta {
                sign: "-".into(),
                kind: "function".into(),
                name: "old".into(),
                line: String::new(),
            },
            DeclarationDelta {
                sign: "+".into(),
                kind: "function".into(),
                name: "new".into(),
                line: String::new(),
            },
        ];
        assert!(operation_hints(&decls, 1, 1, 1, "@@ -1 +1 @@").contains(&"rename-symbol".into()));
    }

    #[test]
    fn hints_multi_patch_for_multiple_hunks_in_one_file() {
        let hints = operation_hints(&[], 2, 2, 1, "@@ -1 +1 @@\n-a\n+b\n@@ -8 +8 @@\n-c\n+d");
        assert!(hints.contains(&"multi-patch".into()));
    }

    #[test]
    fn rejects_obvious_release_and_documentation_noise() {
        for summary in [
            "Release version 1.2.3",
            "docs: clarify parser behavior",
            "fix typo in comment",
            "format all files with gofmt",
        ] {
            assert!(NOISE_SUMMARY.is_match(summary), "{summary}");
        }
        assert!(!NOISE_SUMMARY.is_match("Fix proxy panic with empty target"));
    }
}
