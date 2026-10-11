//! Port of writes/delete_symbol.py and writes/replace_symbol.py — the
//! index-driven symbol-level edits. Language-specific bits (parsing new
//! content, PEP 8 blank-gap normalization) go through the daemon adapter.

use std::path::Path;

use anyhow::Result;

use crate::adapters::Registry;
use crate::index::{Index, S_EBYTE, S_SBYTE};
use crate::writes::content::{indent_at_byte, normalize_content};
use crate::writes::patch::{apply_patch, line_end_byte, line_start_byte, PatchRequest};
use crate::writes::read_cache::ReadCache;
use crate::writes::tx::{commit_edits, FileEdit};

fn ident_ok(name: &str) -> bool {
    static IDENT_RE: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"^[A-Za-z_][A-Za-z_0-9]*$").unwrap());
    IDENT_RE.is_match(name)
}

/// Resolve a qualified path to exactly one live row, with ensure_fresh
/// re-resolution — the shared preamble of delete/replace/rename.
pub(crate) fn resolve_single(
    index: &mut Index,
    qualified_path: &str,
    reg: &Registry,
) -> Result<i64, serde_json::Value> {
    let rows: Vec<i64> = index
        .by_path
        .get(qualified_path)
        .cloned()
        .unwrap_or_default();
    if rows.is_empty() {
        return Err(serde_json::json!({
            "status": "error", "error_code": "symbol_not_found",
            "message": format!("no symbol at {qualified_path:?}"),
        }));
    }
    if rows.len() > 1 {
        let candidates: Vec<String> = rows
            .iter()
            .map(|&r| {
                format!(
                    "{}:{}-{}",
                    index.file_of(r),
                    index.range_of(r).0,
                    index.range_of(r).1
                )
            })
            .collect();
        return Err(serde_json::json!({
            "status": "error", "error_code": "symbol_ambiguous",
            "message": format!("{} symbols match {qualified_path:?}", rows.len()),
            "candidates": candidates,
        }));
    }
    let mut row = rows[0];
    let file_rel = index.file_of(row).to_owned();
    if index.ensure_fresh(&file_rel, reg) {
        let rows: Vec<i64> = index
            .by_path
            .get(qualified_path)
            .cloned()
            .unwrap_or_default();
        if rows.is_empty() {
            return Err(serde_json::json!({
                "status": "error", "error_code": "symbol_not_found",
                "message": format!("symbol {qualified_path:?} no longer exists after refresh"),
            }));
        }
        row = rows[0];
    }
    Ok(row)
}

fn normalize_blank_gaps_via_adapter(file_abs: &Path, language: &str, reg: &Registry) {
    let Ok(adapter) = reg.for_language(language) else {
        return;
    };
    let Ok(source) = std::fs::read(file_abs) else {
        return;
    };
    let text = String::from_utf8_lossy(&source).into_owned();
    if let Ok(normalized) = adapter.normalize_blank_gaps(&text) {
        if normalized != text {
            let _ = crate::writes::patch::atomic_write(file_abs, normalized.as_bytes());
        }
    }
}

// ---------------------------------------------------------- delete-symbol

pub fn delete_symbol(
    index: &mut Index,
    qualified_path: &str,
    project_root: &Path,
    reg: &Registry,
    force: bool,
    dry_run: bool,
) -> serde_json::Value {
    let row = match resolve_single(index, qualified_path, reg) {
        Ok(r) => r,
        Err(e) => return e,
    };
    let file_rel = index.file_of(row).to_owned();
    let file_abs = project_root.join(&file_rel);
    let line_range = index.range_of(row);
    let byte_range = (
        index.symbols[row as usize][S_SBYTE] as usize,
        index.symbols[row as usize][S_EBYTE] as usize,
    );
    let kind = index.kind_of(row).to_owned();
    let language = index.language_of(row).to_owned();

    let leaf = qualified_path.rsplit('.').next().unwrap_or(qualified_path);
    let callers: Vec<serde_json::Value> = index
        .callers_of(leaf)
        .into_iter()
        .filter(|&(src_row, _, _)| src_row != row)
        .map(|(src_row, line, kind_label)| {
            serde_json::json!({
                "file": index.file_of(src_row),
                "source_path": index.path_of(src_row),
                "line": line,
                "kind": kind_label,
            })
        })
        .collect();

    if !callers.is_empty() && !force {
        return serde_json::json!({
            "status": "error", "error_code": "has_live_references",
            "message": format!(
                "{qualified_path} is referenced by {} other symbol(s); use --force to delete anyway",
                callers.len()
            ),
            "qualified_path": qualified_path, "kind": kind,
            "file_rel": file_rel, "line_range": line_range, "callers": callers,
        });
    }

    let cache = ReadCache::for_session(project_root);
    let patch_req = PatchRequest {
        file_abs: file_abs.clone(),
        file_rel: file_rel.clone(),
        line_range,
        byte_range,
        content: vec![],
        force: true, // delete-symbol bypasses the cache check by design
    };
    let result = apply_patch(&patch_req, &cache, dry_run, 5);
    if result.status == "applied" {
        normalize_blank_gaps_via_adapter(&file_abs, &language, reg);
        cache.invalidate(&file_rel);
    }
    if result.status == "error" {
        return serde_json::json!({
            "status": "error", "error_code": result.error_code, "message": result.message,
            "qualified_path": qualified_path, "file_rel": file_rel,
        });
    }
    serde_json::json!({
        "status": result.status,
        "qualified_path": qualified_path,
        "kind": kind,
        "file_rel": file_rel,
        "line_range": line_range,
        "callers": callers,
        "diff": result.diff,
        "lines_removed": result.lines_removed,
    })
}

// ---------------------------------------------------------- insert-symbol

/// Kinds that have a body (start/end are valid) and can carry decorators —
/// mirrors insert_symbol.py's has_body set.
const HAS_BODY: &[&str] = &[
    "class",
    "function",
    "async_function",
    "method",
    "async_method",
];

/// Port of writes/insert_symbol.py — add content at a position anchored to an
/// existing symbol via a zero-width byte splice through the patch engine.
#[allow(clippy::too_many_arguments)]
pub fn insert_symbol(
    index: &mut Index,
    anchor_path: &str,
    position: &str,
    content: &str,
    reindent: bool,
    project_root: &Path,
    reg: &Registry,
    dry_run: bool,
) -> serde_json::Value {
    let row = match resolve_single(index, anchor_path, reg) {
        Ok(r) => r,
        Err(e) => return e,
    };
    let file_rel = index.file_of(row).to_owned();
    let file_abs = project_root.join(&file_rel);
    let (start_line, end_line) = index.range_of(row);
    let kind = index.kind_of(row).to_owned();
    let language = index.language_of(row).to_owned();

    // Shared error shape for the resolution/argument phase (before we have a
    // patch result). Mirrors InsertSymbolResult's error fields.
    let arg_err = |code: &str, msg: String| {
        serde_json::json!({
            "status": "error", "error_code": code, "message": msg,
            "anchor_path": anchor_path, "anchor_kind": kind,
            "position": position, "file_rel": file_rel,
        })
    };

    let has_body = HAS_BODY.contains(&kind.as_str());
    if (position == "start" || position == "end") && !has_body {
        return arg_err(
            "invalid_argument",
            format!(
                "position {position:?} requires a symbol with a body; {anchor_path} is {kind:?}"
            ),
        );
    }

    let source = match std::fs::read(&file_abs) {
        Ok(s) => s,
        Err(e) => {
            return arg_err("file_not_found", format!("cannot read {file_rel}: {e}"));
        }
    };

    let anchor_indent = indent_at_byte(&source, line_start_byte(&source, start_line));
    let body_indent = format!("{anchor_indent}    "); // one step deeper

    let (insert_line, insert_byte, target_indent) = match position {
        "before" => {
            // If the anchor carries decorators, land above the first decorator
            // — not between decorator(s) and `def`, which would break the
            // binding. Only Python defs/classes carry decorators; for other
            // languages/kinds the daemon call is skipped (matches the Python
            // ast walk falling through to def_line).
            let anchor_top = if language == "python" && has_body {
                reg.for_language(&language)
                    .ok()
                    .and_then(|a| a.first_decorator_line(&file_abs, start_line).ok())
                    .unwrap_or(start_line)
            } else {
                start_line
            };
            (
                anchor_top,
                line_start_byte(&source, anchor_top),
                anchor_indent.clone(),
            )
        }
        "after" => (
            end_line + 1,
            line_end_byte(&source, end_line),
            anchor_indent.clone(),
        ),
        "start" => (
            start_line + 1,
            line_end_byte(&source, start_line),
            body_indent.clone(),
        ),
        "end" => (
            end_line + 1,
            line_end_byte(&source, end_line),
            body_indent.clone(),
        ),
        other => {
            return arg_err(
                "invalid_argument",
                format!("position must be one of before/after/start/end, got {other:?}"),
            );
        }
    };

    let payload = if reindent {
        normalize_content(content, &target_indent)
    } else {
        content.as_bytes().to_vec()
    };

    let cache = ReadCache::for_session(project_root);
    let patch_req = PatchRequest {
        file_abs: file_abs.clone(),
        file_rel: file_rel.clone(),
        line_range: (insert_line, insert_line),
        byte_range: (insert_byte, insert_byte),
        content: payload,
        force: true,
    };
    let result = apply_patch(&patch_req, &cache, dry_run, 5);
    if result.status == "applied" {
        normalize_blank_gaps_via_adapter(&file_abs, &language, reg);
        cache.invalidate(&file_rel);
    }
    if result.status == "error" {
        return serde_json::json!({
            "status": "error", "error_code": result.error_code, "message": result.message,
            "anchor_path": anchor_path, "anchor_kind": kind,
            "position": position, "file_rel": file_rel, "insert_line": insert_line,
        });
    }
    serde_json::json!({
        "status": result.status,
        "anchor_path": anchor_path,
        "anchor_kind": kind,
        "position": position,
        "file_rel": file_rel,
        "insert_line": insert_line,
        "diff": result.diff,
        "lines_added": result.lines_added,
    })
}

// ---------------------------------------------------------- replace-symbol

fn kinds_compatible(old_kind: &str, new_kind: &str) -> bool {
    if old_kind == new_kind {
        return true;
    }
    // All callable kinds form ONE family: a method replacement is authored as
    // a bare top-level `def` (so it scans as `function` even when the target
    // is a `method`). Non-callable kinds must match exactly.
    const CALLABLE: [&str; 4] = ["function", "async_function", "method", "async_method"];
    CALLABLE.contains(&old_kind) && CALLABLE.contains(&new_kind)
}

const CALLABLE_KINDS: [&str; 4] = ["function", "async_function", "method", "async_method"];

/// Conservative positional pairing of old→new parameter lists. Returns the
/// list of `(old, new)` renames, or None if the edit is NOT a clean rename set
/// (arity changed, or a swap/reorder). An empty vec means identical lists.
fn detect_param_renames(old: &[String], new: &[String]) -> Option<Vec<(String, String)>> {
    if old.len() != new.len() {
        return None;
    }
    let old_set: std::collections::HashSet<&String> = old.iter().collect();
    let new_set: std::collections::HashSet<&String> = new.iter().collect();
    let mut renames = vec![];
    for (o, n) in old.iter().zip(new.iter()) {
        if o == n {
            continue;
        }
        if old_set.contains(n) || new_set.contains(o) {
            return None; // swap / reorder / ambiguous
        }
        renames.push((o.clone(), n.clone()));
    }
    Some(renames)
}

/// Python's textwrap.dedent (used before parsing the new content): remove
/// the longest common leading whitespace from all non-blank lines.
fn dedent(text: &str) -> String {
    let mut prefix: Option<&str> = None;
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let ws = &line[..line.len() - line.trim_start().len()];
        prefix = Some(match prefix {
            None => ws,
            Some(p) => {
                let common: usize = p
                    .bytes()
                    .zip(ws.bytes())
                    .take_while(|(a, b)| a == b)
                    .count();
                &p[..common]
            }
        });
    }
    let prefix = prefix.unwrap_or("");
    text.lines()
        .map(|l| {
            if l.trim().is_empty() {
                l
            } else {
                l.strip_prefix(prefix).unwrap_or(l)
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn replace_symbol(
    index: &mut Index,
    qualified_path: &str,
    content: &str,
    project_root: &Path,
    reg: &Registry,
    dry_run: bool,
) -> serde_json::Value {
    let row = match resolve_single(index, qualified_path, reg) {
        Ok(r) => r,
        Err(e) => return e,
    };
    let declaring_file_rel = index.file_of(row).to_owned();
    let declaring_file_abs = project_root.join(&declaring_file_rel);
    let old_leaf = qualified_path
        .rsplit('.')
        .next()
        .unwrap_or(qualified_path)
        .to_owned();
    let old_kind = index.kind_of(row).to_owned();
    let language = index.language_of(row).to_owned();
    let old_byte_range = (
        index.symbols[row as usize][S_SBYTE] as usize,
        index.symbols[row as usize][S_EBYTE] as usize,
    );

    let err = |code: &str, msg: String| serde_json::json!({ "status": "error", "error_code": code, "message": msg });

    let Ok(adapter) = reg.for_language(&language) else {
        return err("no_adapter", format!("no adapter for {language}"));
    };
    let parse_text = dedent(content);
    match adapter.validate_syntax(&parse_text) {
        Ok(parse) if !parse.ok => {
            return err(
                "parse_broken",
                format!(
                    "new content doesn't parse: line {}: {}",
                    parse.error_line.unwrap_or(0),
                    parse.error_message.unwrap_or_default()
                ),
            )
        }
        Err(e) => return err("no_adapter", format!("{e}")),
        _ => {}
    }
    let scan = match adapter.scan_content(&parse_text) {
        Ok(s) => s,
        Err(e) => return err("parse_broken", format!("{e}")),
    };
    if scan.symbols.is_empty() {
        return err(
            "invalid_argument",
            "new content contains no top-level symbol definitions".into(),
        );
    }
    if scan.symbols.len() > 1 {
        return err(
            "invalid_argument",
            format!(
                "new content has {} top-level definitions; replace-symbol needs exactly one",
                scan.symbols.len()
            ),
        );
    }
    let new_sym = &scan.symbols[0];
    let new_leaf = new_sym.name.clone();
    if !ident_ok(&new_leaf) {
        return err(
            "invalid_argument",
            format!("new symbol name {new_leaf:?} is not a valid identifier"),
        );
    }
    if !kinds_compatible(&old_kind, &new_sym.kind) {
        return err(
            "invalid_argument",
            format!(
                "kind mismatch: old symbol is {old_kind:?}, new content defines a {:?}",
                new_sym.kind
            ),
        );
    }

    let name_changed = new_leaf != old_leaf;
    let prefix = &qualified_path[..qualified_path.len() - old_leaf.len()];
    let new_qpath = if prefix.is_empty() {
        new_leaf.clone()
    } else {
        format!("{prefix}{new_leaf}")
    };
    if name_changed && index.by_path.contains_key(&new_qpath) {
        return err("name_collision", format!("{new_qpath:?} already exists"));
    }

    let declaring_source = match std::fs::read(&declaring_file_abs) {
        Ok(s) => s,
        Err(e) => {
            return err(
                "file_not_found",
                format!("cannot read {declaring_file_rel}: {e}"),
            )
        }
    };
    let (start, end) = old_byte_range;
    if end > declaring_source.len() {
        return err(
            "conflict",
            format!("declaring file shrank: byte range {start}-{end} exceeds current size"),
        );
    }

    let pattern = name_changed
        .then(|| regex::Regex::new(&format!(r"\b{}\b", regex::escape(&old_leaf))).unwrap());
    let mut before = declaring_source[..start].to_vec();
    let mut after = declaring_source[end..].to_vec();
    let mut declaring_refs_updated = 1i64;
    if let Some(pat) = &pattern {
        let before_text = String::from_utf8_lossy(&before).into_owned();
        let c1 = pat.find_iter(&before_text).count() as i64;
        before = pat
            .replace_all(&before_text, new_leaf.as_str())
            .into_owned()
            .into_bytes();
        let after_text = String::from_utf8_lossy(&after).into_owned();
        let c2 = pat.find_iter(&after_text).count() as i64;
        after = pat
            .replace_all(&after_text, new_leaf.as_str())
            .into_owned()
            .into_bytes();
        declaring_refs_updated += c1 + c2;
    }

    let target_indent = indent_at_byte(&declaring_source, start);
    let content_bytes = normalize_content(content, &target_indent);
    let mut declaring_new = Vec::with_capacity(before.len() + content_bytes.len() + after.len());
    declaring_new.extend_from_slice(&before);
    declaring_new.extend_from_slice(&content_bytes);
    declaring_new.extend_from_slice(&after);

    let mut edits = vec![FileEdit {
        file_abs: declaring_file_abs.clone(),
        file_rel: declaring_file_rel.clone(),
        new_content: declaring_new,
    }];
    let mut per_file = vec![serde_json::json!({
        "file": declaring_file_rel, "refs_updated": declaring_refs_updated,
    })];

    if let Some(pat) = &pattern {
        let mut other_files: std::collections::BTreeSet<String> = index
            .callers_of(&old_leaf)
            .into_iter()
            .map(|(src_row, _, _)| index.file_of(src_row).to_owned())
            .collect();
        other_files.remove(&declaring_file_rel);
        for rel in other_files {
            let abs = project_root.join(&rel);
            let Ok(src) = std::fs::read(&abs) else {
                continue;
            };
            let text = String::from_utf8_lossy(&src).into_owned();
            let count = pat.find_iter(&text).count() as i64;
            if count == 0 {
                continue;
            }
            let new_text = pat.replace_all(&text, new_leaf.as_str()).into_owned();
            edits.push(FileEdit {
                file_abs: abs,
                file_rel: rel.clone(),
                new_content: new_text.into_bytes(),
            });
            per_file.push(serde_json::json!({ "file": rel, "refs_updated": count }));
        }
    }

    // Parameter renames are a CONSEQUENCE of a signature edit, not a verb.
    // For function-family python targets, compare the OLD def's parameter list
    // (sliced) against the NEW content's; unambiguous positional renames are
    // propagated to keyword-argument call sites, folded into the SAME tx.
    let mut parameter_renames_json: Vec<serde_json::Value> = vec![];
    let mut signature_changed = false;
    let mut old_params_json: Vec<String> = vec![];
    let mut new_params_json: Vec<String> = vec![];

    if language == "python" && CALLABLE_KINDS.contains(&old_kind.as_str()) {
        let old_slice = dedent(&String::from_utf8_lossy(&declaring_source[start..end]));
        let old_params = adapter
            .function_parameters(&declaring_file_abs, &old_leaf, None, Some(&old_slice))
            .ok()
            .flatten();
        let new_params = adapter
            .function_parameters(&declaring_file_abs, &new_leaf, None, Some(&parse_text))
            .ok()
            .flatten();
        if let (Some(op), Some(np)) = (old_params, new_params) {
            match detect_param_renames(&op, &np) {
                None => {
                    if op != np {
                        signature_changed = true;
                        old_params_json = op;
                        new_params_json = np;
                    }
                }
                Some(renames) if !renames.is_empty() => {
                    // The daemon resolves against the ON-DISK index; sync it.
                    let _ = index.save();

                    let mut candidates: std::collections::BTreeSet<String> =
                        std::collections::BTreeSet::from([declaring_file_rel.clone()]);
                    for (src_row, _l, _k) in index.callers_of(&old_leaf) {
                        candidates.insert(index.file_of(src_row).to_owned());
                    }
                    for file_id in 0..index.files.len() as i64 {
                        if index
                            .imports_for(file_id)
                            .iter()
                            .any(|(local, _, _)| *local == old_leaf)
                        {
                            candidates.insert(index.files[file_id as usize].clone());
                        }
                    }

                    // Pending content seeded from staged edits (declaring
                    // splice + any cross-file function-name renames).
                    let mut pending: std::collections::BTreeMap<String, Vec<u8>> = edits
                        .iter()
                        .map(|e| (e.file_rel.clone(), e.new_content.clone()))
                        .collect();
                    let mut order: Vec<String> = edits.iter().map(|e| e.file_rel.clone()).collect();
                    let mut agg: Vec<(String, String, i64, i64, i64)> = renames
                        .iter()
                        .map(|(o, n)| (o.clone(), n.clone(), 0, 0, 0))
                        .collect();

                    for rel in &candidates {
                        if index.language_of_file(rel) != Some("python") {
                            continue;
                        }
                        let abs = project_root.join(rel);
                        let mut current = match pending.get(rel) {
                            Some(c) => c.clone(),
                            None => match std::fs::read(&abs) {
                                Ok(s) => s,
                                Err(_) => continue,
                            },
                        };
                        let is_declaring = rel == &declaring_file_rel;
                        let mut changed = false;
                        for (i, (o, n)) in renames.iter().enumerate() {
                            let src_str = String::from_utf8_lossy(&current).into_owned();
                            let analysis = match adapter.rename_parameter_analysis(
                                &abs,
                                &new_qpath,
                                &new_leaf,
                                o,
                                is_declaring,
                                None,
                                n,
                                true,
                                Some(&src_str),
                            ) {
                                Ok(a) => a,
                                Err(e) => {
                                    return err(
                                        "adapter_failed",
                                        format!("parameter propagation failed for {rel}: {e}"),
                                    )
                                }
                            };
                            agg[i].2 += analysis.rewrites.len() as i64;
                            agg[i].3 += analysis.skipped_mismatch.len() as i64;
                            agg[i].4 += analysis.unresolved.len() as i64;
                            if !analysis.rewrites.is_empty() {
                                let mut sorted = analysis.rewrites.clone();
                                sorted.sort_by_key(|r| std::cmp::Reverse(r.byte_start));
                                for r in &sorted {
                                    if r.byte_end <= current.len() && r.byte_start <= r.byte_end {
                                        current
                                            .splice(r.byte_start..r.byte_end, r.new_text.bytes());
                                    }
                                }
                                changed = true;
                            }
                        }
                        if changed {
                            if !pending.contains_key(rel) {
                                order.push(rel.clone());
                            }
                            pending.insert(rel.clone(), current);
                        }
                    }

                    edits = order
                        .iter()
                        .map(|rel| FileEdit {
                            file_abs: project_root.join(rel),
                            file_rel: rel.clone(),
                            new_content: pending.get(rel).cloned().unwrap_or_default(),
                        })
                        .collect();

                    for (o, n, refs, skip, unres) in &agg {
                        parameter_renames_json.push(serde_json::json!({
                            "old": o, "new": n, "refs_updated": refs,
                            "skipped_mismatch": skip, "unresolved": unres,
                        }));
                    }
                }
                Some(_) => {} // empty rename set: a pure body edit
            }
        }
    }

    let subject = if name_changed {
        format!("{qualified_path} → {new_leaf} (rewrite + rename)")
    } else {
        format!("{qualified_path} (rewrite)")
    };
    let tx = commit_edits(&edits, project_root, "replace-symbol", &subject, dry_run);
    if tx.status == "error" {
        return serde_json::json!({
            "status": "error", "error_code": tx.error_code, "message": tx.message,
            "qualified_path": qualified_path,
        });
    }
    if !dry_run {
        normalize_blank_gaps_via_adapter(&declaring_file_abs, &language, reg);
    }

    let total_refs: i64 = per_file
        .iter()
        .map(|f| f["refs_updated"].as_i64().unwrap_or(0))
        .sum();
    let new_sig = parse_text
        .lines()
        .nth((new_sym.line_range.0 - 1).max(0) as usize)
        .unwrap_or("")
        .trim();
    serde_json::json!({
        "status": if dry_run { "dry_run" } else { "applied" },
        "qualified_path": qualified_path,
        "new_qualified_path": new_qpath,
        "name_changed": name_changed,
        "declaring_file": declaring_file_rel,
        "new_signature": new_sig,
        "kind": old_kind,
        "files_changed": edits.len(),
        "refs_updated": total_refs,
        "per_file": per_file,
        "parameter_renames": parameter_renames_json,
        "signature_changed": signature_changed,
        "old_parameters": old_params_json,
        "new_parameters": new_params_json,
        "transaction_id": tx.transaction_id,
    })
}
