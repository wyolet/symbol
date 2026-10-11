//! Port of reads/ — search, code, outline, callers. Pure index queries;
//! signature/preview delegate to the language daemon like Python.

use std::path::Path;

use anyhow::{bail, Result};

use crate::adapters::Registry;
use crate::index::{Index, S_SLINE};

type Matcher = Box<dyn Fn(&str) -> bool>;
type Matchers = Vec<Matcher>;

// ---------------------------------------------------------- callers

pub fn callers(index: &Index, name: &str) -> Vec<serde_json::Value> {
    let target = name.rsplit('.').next().unwrap_or(name);
    index
        .callers_of(target)
        .into_iter()
        .map(|(source_row, line, kind)| {
            let (s, e) = index.range_of(source_row);
            serde_json::json!({
                "source_path": index.path_of(source_row),
                "source_file": index.file_of(source_row),
                "source_range": [s, e],
                "ref_name": target,
                "ref_kind": kind,
                "ref_line": line,
            })
        })
        .collect()
}

// ---------------------------------------------------------- code

pub fn code(index: &Index, target: &str, reg: &Registry) -> Result<serde_json::Value> {
    static RANGE_RE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"^(?P<file>.+?):(?P<start>\d+)-(?P<end>\d+)$").unwrap()
    });

    if let Some(m) = RANGE_RE.captures(target) {
        return by_range(
            index,
            &m["file"],
            m["start"].parse()?,
            m["end"].parse()?,
            reg,
        );
    }
    by_path(index, target, reg)
}

fn resolve_rows(index: &Index, path: &str) -> Vec<i64> {
    let mut rows: Vec<i64> = index.by_path.get(path).cloned().unwrap_or_default();
    if rows.is_empty() {
        let suffix = format!(".{path}");
        for (qpath, ids) in &index.by_path {
            if qpath == path || qpath.ends_with(&suffix) {
                rows.extend(ids);
            }
        }
    }
    rows
}

fn by_path(index: &Index, path: &str, reg: &Registry) -> Result<serde_json::Value> {
    let rows = resolve_rows(index, path);
    if rows.is_empty() {
        bail!("no symbol matches {path:?}\nTry: symbol search <name>");
    }
    if rows.len() > 1 {
        let candidates: Vec<serde_json::Value> = rows
            .iter()
            .map(|&r| {
                serde_json::json!({
                    "path": index.path_of(r),
                    "file": index.file_of(r),
                    "start_line": index.range_of(r).0,
                    "end_line": index.range_of(r).1,
                    "kind": index.kind_of(r),
                    "signature": index.signature(r, reg),
                })
            })
            .collect();
        return Ok(serde_json::json!({
            "ambiguous": true,
            "candidates": candidates,
        }));
    }
    Ok(index.row_payload(rows[0], reg))
}

fn by_range(
    index: &Index,
    file: &str,
    start: i64,
    end: i64,
    reg: &Registry,
) -> Result<serde_json::Value> {
    let mut file = file.to_owned();
    let mut file_id = index.file_id_of(&file);
    if file_id.is_none() {
        let as_path = Path::new(&file);
        if as_path.exists() {
            if let Ok(abs) = as_path.canonicalize() {
                if let Ok(rel) = abs.strip_prefix(&index.project_root) {
                    let rel = rel.to_string_lossy().into_owned();
                    if let Some(fid) = index.file_id_of(&rel) {
                        file = rel;
                        file_id = Some(fid);
                    }
                }
            }
        }
    }
    let Some(file_id) = file_id else {
        bail!("file not indexed: {file:?}");
    };

    let mut best_row: i64 = -1;
    let mut best_span: Option<(i64, i64)> = None;
    for &row in index
        .by_file
        .get(&file_id)
        .map(Vec::as_slice)
        .unwrap_or(&[])
    {
        let (rs, re_) = index.range_of(row);
        if rs == start && re_ == end {
            best_row = row;
            break;
        }
        if rs <= start && re_ >= end {
            let span = re_ - rs;
            if best_span.is_none_or(|(bs, be)| span < be - bs) {
                best_row = row;
                best_span = Some((rs, re_));
            }
        }
    }
    if best_row == -1 {
        return Ok(index.raw_slice(&file, start, end));
    }
    let mut payload = index.row_payload(best_row, reg);
    if payload["start_line"] != start || payload["end_line"] != end {
        payload["note"] = format!(
            "requested {file}:{start}-{end} is inside {} ({}-{})",
            payload["path"].as_str().unwrap_or(""),
            payload["start_line"],
            payload["end_line"],
        )
        .into();
    }
    Ok(payload)
}

// ---------------------------------------------------------- outline

pub fn outline(index: &Index, query: &str, reg: &Registry) -> Vec<serde_json::Value> {
    if index.file_id_of(query).is_some() {
        return outline_file(index, query, reg);
    }
    if query.contains('/')
        || [".py", ".ts", ".js", ".go", ".php"]
            .iter()
            .any(|e| query.ends_with(e))
    {
        return outline_file(index, query, reg);
    }
    outline_symbol(index, query, reg)
}

pub fn outline_file(index: &Index, file: &str, reg: &Registry) -> Vec<serde_json::Value> {
    let Some(file_id) = index.file_id_of(file) else {
        return vec![];
    };
    let mut row_ids: Vec<i64> = index.by_file.get(&file_id).cloned().unwrap_or_default();
    row_ids.sort_by_key(|&i| index.symbols[i as usize][S_SLINE]);
    index.build_tree(&row_ids, None, reg)
}

pub fn outline_symbol(index: &Index, query: &str, reg: &Registry) -> Vec<serde_json::Value> {
    let row_ids = resolve_rows(index, query);
    if row_ids.is_empty() {
        return vec![];
    }
    let mut out = vec![];
    for root_row in row_ids {
        let mut descendants = vec![root_row];
        descendants.extend(index.descendants_of(root_row));
        descendants.sort_by_key(|&i| index.symbols[i as usize][S_SLINE]);
        out.extend(index.build_tree(&descendants, Some(root_row), reg));
    }
    out
}

// ---------------------------------------------------------- search

pub struct SearchOpts<'a> {
    pub kind: Option<&'a str>,
    pub file: Option<&'a str>,
    pub regex: bool,
    pub fixed: bool,
    pub ignore_case: bool,
    pub limit: usize,
}

impl Default for SearchOpts<'_> {
    fn default() -> Self {
        SearchOpts {
            kind: None,
            file: None,
            regex: false,
            fixed: false,
            ignore_case: false,
            limit: 100,
        }
    }
}

const PREVIEW_LINES: usize = 3;

pub fn search(
    index: &Index,
    patterns: &[String],
    opts: &SearchOpts,
    reg: &Registry,
) -> Result<Vec<serde_json::Value>> {
    if patterns.is_empty() {
        return Ok(vec![]);
    }
    let matchers: Result<Matchers> = patterns.iter().map(|p| compile_matcher(p, opts)).collect();
    let matchers = matchers?;

    let mut row_ids: Vec<i64> = vec![];
    let mut seen = std::collections::HashSet::new();
    // Iteration order must match Python's: by_path is a dict whose insertion
    // order is first-seen symbol order during ingest. Row ids are identical
    // across implementations, so ordering by each path's first row id
    // reproduces it exactly (alphabetical sort diverges once `limit`
    // truncates — WP7 sweep finding sr058).
    let mut paths: Vec<&String> = index.by_path.keys().collect();
    paths.sort_by_key(|p| index.by_path[*p].first().copied().unwrap_or(i64::MAX));
    for path in paths {
        if !matchers.iter().all(|m| m(path)) {
            continue;
        }
        for &rid in &index.by_path[path] {
            if seen.insert(rid) {
                row_ids.push(rid);
            }
        }
    }

    let mut out = vec![];
    for row in row_ids {
        if let Some(kind) = opts.kind {
            if index.kind_of(row) != kind {
                continue;
            }
        }
        let file_path = index.file_of(row).to_owned();
        if let Some(f) = opts.file {
            if f != file_path {
                continue;
            }
        }
        let (s, e) = index.range_of(row);
        let signature = index.signature(row, reg);
        let body = index.body(row);
        let preview = reg
            .for_language(index.language_of(row))
            .ok()
            .and_then(|a| a.preview(&body, &signature, PREVIEW_LINES).ok())
            .unwrap_or_else(|| {
                body.lines()
                    .take(PREVIEW_LINES)
                    .collect::<Vec<_>>()
                    .join("\n")
            });
        out.push(serde_json::json!({
            "path": index.path_of(row),
            "file": file_path,
            "start_line": s,
            "end_line": e,
            "kind": index.kind_of(row),
            "language": index.language_of(row),
            "signature": signature,
            "preview": preview,
        }));
        if out.len() >= opts.limit {
            break;
        }
    }
    Ok(out)
}

fn compile_matcher(pattern: &str, opts: &SearchOpts) -> Result<Matcher> {
    if opts.regex {
        let p = if opts.ignore_case {
            format!("(?i){pattern}")
        } else {
            pattern.to_owned()
        };
        let rx = regex::Regex::new(&p)?;
        return Ok(Box::new(move |s: &str| rx.is_match(s)));
    }
    if opts.fixed {
        if opts.ignore_case {
            let needle = pattern.to_lowercase();
            return Ok(Box::new(move |s: &str| s.to_lowercase().contains(&needle)));
        }
        let needle = pattern.to_owned();
        return Ok(Box::new(move |s: &str| s.contains(&needle)));
    }
    let suffix = format!(".{pattern}");
    if opts.ignore_case {
        let target = pattern.to_lowercase();
        let suffix_l = suffix.to_lowercase();
        return Ok(Box::new(move |s: &str| {
            let l = s.to_lowercase();
            l == target || l.ends_with(&suffix_l)
        }));
    }
    let target = pattern.to_owned();
    Ok(Box::new(move |s: &str| s == target || s.ends_with(&suffix)))
}
