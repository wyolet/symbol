//! MCP tool implementations. Reads return JSON objects; xref reads and writes
//! return agent-format text.

use std::collections::HashSet;
use std::path::Path;

use anyhow::{anyhow, Result};
use serde_json::{json, Map, Value};

use crate::adapters::{self, Registry};
use crate::index::Index;
use crate::mcp::state::{relative_to, resolve, FileStatus, Session};
use crate::mcp::tools::Args;
use crate::pystr;
use crate::reads::{self, CodeMiss, SearchOpts};
use crate::render::agent;
use crate::writes::ops::{delete_symbol_with_cache, insert_symbol_with_cache, replace_symbol};
use crate::writes::patch::{
    apply_multi_patch, apply_patch, preflight, validate_args, MultiPatchEdit,
};
use crate::writes::read_cache::record_served;
use crate::writes::rename::rename_symbol;
use crate::writes::tx::undo_last;
use crate::xref;

pub enum ToolOutput {
    Json(Value),
    Text(String),
}

/// `_capture_text`: what an agent renderer printed, trailing newlines off.
fn captured(printed: String) -> String {
    printed.trim_end_matches('\n').to_owned()
}

pub fn call(name: &str, session: &mut Session, args: &Args) -> Result<ToolOutput> {
    match name {
        "SearchSymbol" => search_symbol(session, args),
        "SymbolBody" => symbol_body(session, args),
        "SymbolOutline" => symbol_outline(session, args),
        "SymbolCallers" => symbol_callers(session, args),
        "SymbolRefs" => xref(session, args, xref::references, agent::references),
        "SymbolImplementers" => xref(session, args, xref::implementers, agent::implementers),
        "Patch" => patch(session, args),
        "MultiPatch" => multi_patch(session, args),
        "DeleteSymbol" => delete(session, args),
        "InsertSymbol" => insert(session, args),
        "RenameSymbol" => rename(session, args),
        "Refresh" => refresh(session, args),
        "Undo" => undo(session),
        "ReplaceSymbol" => replace(session, args),
        other => Err(anyhow!("Unknown tool: {other}")),
    }
}

fn error_json(code: &str, message: String) -> Value {
    json!({"ok": false, "error_code": code, "message": message})
}

/// `_ensure_or_error_dict`: None when `file` is indexed (or just got indexed).
fn ensure_or_error(session: &mut Session, file: &str) -> Result<Option<Value>> {
    Ok(match session.ensure_indexed(file)? {
        FileStatus::Ok(_) => None,
        FileStatus::NotFound(_) => Some(error_json("not_found", format!("file not found: {file}"))),
        FileStatus::OutsideProject => Some(error_json(
            "invalid_argument",
            format!("file is outside project root: {file}"),
        )),
        FileStatus::Unsupported(rel) => Some(error_json(
            "unsupported",
            format!("no language adapter for {rel}"),
        )),
    })
}

/// `_ensure_or_error_text`: the same refusal rendered as patch error text.
fn ensure_or_error_text(session: &mut Session, file: &str) -> Result<Option<String>> {
    Ok(ensure_or_error(session, file)?
        .map(|err| captured(agent::patch_error(&err["error_code"], &err["message"]))))
}

/// `_existing_file_part`: the path component of `target` when it names a
/// real file (a trailing `:A-B` line range is split off first).
fn existing_file_part(root: &Path, target: &str) -> Option<String> {
    let base = match target.split_once(':') {
        Some((head, tail))
            if tail.contains('-')
                && !tail.replace('-', "").is_empty()
                && tail.chars().all(|c| c == '-' || c.is_ascii_digit()) =>
        {
            head
        }
        _ => target,
    };
    resolve(&root.join(base)).is_file().then(|| base.to_owned())
}

fn changed(result: &Value) -> bool {
    result["status"] == "applied"
}

/// Files a rename/replace touched: per-file entries, else the declaring file.
fn touched_files(result: &Value) -> HashSet<String> {
    let mut touched: HashSet<String> = result["per_file"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[])
        .iter()
        .filter_map(|f| f["file"].as_str())
        .filter(|f| !f.is_empty())
        .map(str::to_owned)
        .collect();
    if touched.is_empty() {
        if let Some(declaring) = result["declaring_file"].as_str().filter(|f| !f.is_empty()) {
            touched.insert(declaring.to_owned());
        }
    }
    touched
}

/// Auto-append a trailing newline so new definitions don't jam against the
/// next line.
fn with_trailing_newline(content: &str) -> String {
    let mut content = content.to_owned();
    if !content.is_empty() && !content.ends_with('\n') {
        content.push('\n');
    }
    content
}

// ---------------------------------------------------------- reads

fn search_symbol(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let (regex, fixed) = (args.bool("regex"), args.bool("fixed"));
    if regex && fixed {
        return Ok(ToolOutput::Json(error_json(
            "invalid_argument",
            "regex and fixed are mutually exclusive".into(),
        )));
    }
    let file = args.opt_str("file");
    if let Some(file) = file.filter(|f| !f.is_empty()) {
        if let Some(err) = ensure_or_error(session, file)? {
            return Ok(ToolOutput::Json(err));
        }
    }
    let patterns: Vec<String> = args
        .list("patterns")
        .iter()
        .filter_map(|p| p.as_str().map(str::to_owned))
        .collect();
    let reg = session.reg;
    let index = session.index()?;
    let hits = reads::search(
        index,
        &patterns,
        &SearchOpts {
            kind: args.opt_str("kind"),
            file,
            regex,
            fixed,
            ignore_case: args.bool("ignore_case"),
            limit: args.int("limit").max(0) as usize,
        },
        reg,
    )?;
    let status = if index.symbols.is_empty() {
        "empty"
    } else {
        "ok"
    };
    Ok(ToolOutput::Json(json!({
        "ok": true, "count": hits.len(), "hits": hits, "index_status": status,
    })))
}

/// `code_read`, with not-found surfaced as its message.
fn lookup(session: &mut Session, target: &str) -> Result<Result<Value, String>> {
    let reg = session.reg;
    Ok(match reads::lookup_code(session.index()?, target, reg) {
        Ok(hit) => Ok(hit),
        Err(CodeMiss::Ambiguous(candidates)) => {
            Ok(json!({"ambiguous": true, "candidates": candidates}))
        }
        Err(CodeMiss::NotFound(message)) => Err(message),
    })
}

fn ambiguous(hit: &Value) -> Option<Value> {
    if hit["ambiguous"] != true {
        return None;
    }
    let candidates = hit["candidates"].clone();
    let n = candidates.as_array().map_or(0, Vec::len);
    Some(json!({
        "ok": false,
        "error_code": "ambiguous",
        "message": format!("{n} candidates — use symbol search"),
        "candidates": candidates,
    }))
}

fn symbol_body(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let target = args.str("target");
    let hit = match lookup(session, target)? {
        Ok(hit) => hit,
        Err(_) => {
            // The index is the authority on symbols; a miss may still be an
            // unindexed file on disk — index it and retry once.
            let Some(file_part) = existing_file_part(&session.root, target) else {
                return Ok(ToolOutput::Json(error_json(
                    "not_found",
                    format!("no symbol matches {}", pystr::repr(target)),
                )));
            };
            if let Some(err) = ensure_or_error(session, &file_part)? {
                return Ok(ToolOutput::Json(err));
            }
            match lookup(session, target)? {
                Ok(hit) => hit,
                Err(message) => return Ok(ToolOutput::Json(error_json("not_found", message))),
            }
        }
    };
    if let Some(err) = ambiguous(&hit) {
        return Ok(ToolOutput::Json(err));
    }

    let body = hit["body"].as_str().unwrap_or_default();
    let lines = pystr::splitlines(body, true);
    let total = lines.len() as i64;
    let start = args.int("offset").max(0);
    let end = match args.opt_int("limit") {
        None => total,
        Some(limit) => total.min(start + limit.max(0)),
    };
    let (lo, hi) = (start.min(total) as usize, end.clamp(0, total) as usize);
    let sliced: String = if lo < hi {
        lines[lo..hi].concat()
    } else {
        String::new()
    };
    let truncated = (start, end) != (0, total);
    let start_line = hit["start_line"].as_i64().unwrap_or(0);
    let abs_start = start_line + start;
    let abs_end = if end > start {
        start_line + end - 1
    } else {
        start_line + start
    };

    let file = hit["file"].as_str().unwrap_or_default().to_owned();
    record_served(&session.cache, &session.root, &file, abs_start, abs_end);

    let include_refs = args.bool("include_refs");
    let mut out = Map::new();
    out.insert("ok".into(), true.into());
    for (key, value) in hit.as_object().into_iter().flatten() {
        match key.as_str() {
            "body" => {
                out.insert(key.clone(), sliced.clone().into());
            }
            "refs" if !include_refs => {}
            _ => {
                out.insert(key.clone(), value.clone());
            }
        }
    }
    out.insert("total_lines".into(), total.into());
    if truncated {
        out.insert(
            "window".into(),
            json!({"offset": start, "limit": end - start, "abs_lines": [abs_start, abs_end]}),
        );
    }
    Ok(ToolOutput::Json(out.into()))
}

/// Leaf nodes omit `children` entirely.
fn prune_empty_children(nodes: &mut [Value]) {
    for node in nodes {
        let Some(obj) = node.as_object_mut() else {
            continue;
        };
        match obj.get_mut("children").and_then(Value::as_array_mut) {
            Some(children) if !children.is_empty() => prune_empty_children(children),
            _ => {
                obj.shift_remove("children");
            }
        }
    }
}

fn symbol_outline(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let target = args.str("target");
    let reg = session.reg;
    let root = session.root.clone();
    // A path that exists relative to the working directory is addressed by
    // its project-relative form, as the Python server does.
    let mut arg = target.to_owned();
    let candidate = Path::new(target);
    if candidate.exists() {
        if let Some(rel) = relative_to(&resolve(candidate), &root) {
            arg = rel;
        }
    }
    let mut roots = reads::outline(session.index()?, &arg, reg);
    if roots.is_empty() {
        if let Some(file_part) = existing_file_part(&root, target) {
            if let Some(err) = ensure_or_error(session, &file_part)? {
                return Ok(ToolOutput::Json(err));
            }
            arg = file_part;
            roots = reads::outline(session.index()?, &arg, reg);
        }
    }
    prune_empty_children(&mut roots);
    Ok(ToolOutput::Json(
        json!({"ok": true, "target": arg, "roots": roots}),
    ))
}

fn symbol_callers(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let hits = reads::callers(session.index()?, args.str("name"));
    Ok(ToolOutput::Json(
        json!({"ok": true, "count": hits.len(), "hits": hits}),
    ))
}

/// SymbolRefs / SymbolImplementers. An index that can't load (e.g. its
/// daemon is missing) is reported like any other refusal.
fn xref(
    session: &mut Session,
    args: &Args,
    query: fn(&Index, &str, &Registry) -> Value,
    render: fn(&Value, bool) -> String,
) -> Result<ToolOutput> {
    let reg = session.reg;
    let result = match session.index() {
        Ok(index) => query(index, args.str("target"), reg),
        Err(err) => adapters::error_json(&err, "index_error"),
    };
    Ok(ToolOutput::Text(captured(render(
        &result,
        args.bool("verbose"),
    ))))
}

// ---------------------------------------------------------- writes

fn patch(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let file = args.str("file");
    if let Some(err) = ensure_or_error_text(session, file)? {
        return Ok(ToolOutput::Text(err));
    }
    let content = args.opt_str("content").unwrap_or_default();
    let request = match validate_args(
        file,
        args.str("range"),
        content.as_bytes(),
        &session.root,
        args.bool("force"),
    ) {
        Ok(request) => request,
        Err(err) => {
            return Ok(ToolOutput::Text(captured(agent::patch_error(
                &err.error_code.into(),
                &err.message.into(),
            ))))
        }
    };
    if preflight(&request, &session.cache).is_err() {
        // Showing the current bytes is the same proof an explicit read would
        // be, so record them as seen: an identical retry then passes.
        record_served(
            &session.cache,
            &session.root,
            &request.file_rel,
            request.line_range.0,
            request.line_range.1,
        );
        let current = std::fs::read(&request.file_abs)
            .ok()
            .and_then(|data| {
                data.get(request.byte_range.0..request.byte_range.1)
                    .map(|slice| String::from_utf8_lossy(slice).into_owned())
            })
            .unwrap_or_default();
        return Ok(ToolOutput::Text(captured(agent::patch_needs_read(
            &request.file_rel,
            request.line_range,
            request.byte_range,
            &current,
        ))));
    }
    let result = apply_patch(&request, &session.cache, args.bool("dry_run"), 5);
    if result.status == "error" {
        return Ok(ToolOutput::Text(captured(agent::patch_error(
            &result.error_code.into(),
            &result.message.into(),
        ))));
    }
    if result.status == "applied" {
        session.invalidate(HashSet::from([result.file_rel.clone()]));
    }
    Ok(ToolOutput::Text(captured(agent::patch_result(
        &serde_json::to_value(&result)?,
    ))))
}

fn multi_patch(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let file = args.str("file");
    if let Some(err) = ensure_or_error_text(session, file)? {
        return Ok(ToolOutput::Text(err));
    }
    let root = session.root.clone();
    if relative_to(&resolve(&root.join(file)), &root).is_none() {
        return Ok(ToolOutput::Text(captured(agent::patch_error(
            &"invalid_argument".into(),
            &format!("file {} is outside project root", pystr::repr(file)).into(),
        ))));
    }
    let mut edits = vec![];
    for (i, raw) in args.list("edits").iter().enumerate() {
        let field = |key: &str| -> Result<Option<String>> {
            match &raw[key] {
                Value::Null => Ok(None),
                Value::String(s) => Ok(Some(s.clone())),
                other => Err(anyhow!("edit {i}: {key} must be a string, got {other}")),
            }
        };
        edits.push(MultiPatchEdit {
            range: field("range")?,
            old: field("old")?,
            content: field("content")?,
        });
    }
    let result = apply_multi_patch(
        file,
        &edits,
        &root,
        &session.cache,
        args.bool("dry_run"),
        args.bool("force"),
        5,
    );
    if result.status == "applied" {
        session.invalidate(HashSet::from([result.file_rel.clone()]));
    } else if result.status == "needs_read_confirmation" {
        // Same as Patch: the ranges named in the refusal count as seen.
        for u in &result.unconfirmed {
            let Some((start, end)) = u["range"].as_str().and_then(|r| r.split_once('-')) else {
                continue;
            };
            if let (Ok(start), Ok(end)) = (start.parse(), end.parse()) {
                record_served(&session.cache, &root, &result.file_rel, start, end);
            }
        }
    }
    Ok(ToolOutput::Text(agent::multi_patch_result(
        &serde_json::to_value(&result)?,
    )))
}

fn delete(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let (root, reg) = (session.root.clone(), session.reg);
    let (index, cache) = session.index_and_cache()?;
    let result = delete_symbol_with_cache(
        index,
        args.str("target"),
        &root,
        reg,
        args.bool("force"),
        args.bool("dry_run"),
        cache,
        5,
    );
    if let Some(file) = result["file_rel"]
        .as_str()
        .filter(|f| changed(&result) && !f.is_empty())
    {
        session.invalidate(HashSet::from([file.to_owned()]));
    }
    Ok(ToolOutput::Text(captured(agent::delete_symbol(&result))))
}

fn insert(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let position = args.str("position");
    if !["before", "after", "start", "end"].contains(&position) {
        return Ok(ToolOutput::Text(format!(
            "status: error\nerror_code: invalid_argument\n\
             message: position must be one of before|after|start|end, got {}",
            pystr::repr(position)
        )));
    }
    let content = with_trailing_newline(args.str("content"));
    let (root, reg) = (session.root.clone(), session.reg);
    let (index, cache) = session.index_and_cache()?;
    let result = insert_symbol_with_cache(
        index,
        args.str("target"),
        position,
        &content,
        args.bool("reindent"),
        &root,
        reg,
        args.bool("dry_run"),
        cache,
        5,
    );
    if let Some(file) = result["file_rel"]
        .as_str()
        .filter(|f| changed(&result) && !f.is_empty())
    {
        session.invalidate(HashSet::from([file.to_owned()]));
    }
    Ok(ToolOutput::Text(captured(agent::insert_symbol(&result))))
}

fn rename(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let (root, reg) = (session.root.clone(), session.reg);
    let result = rename_symbol(
        session.index()?,
        args.str("target"),
        args.str("new_name"),
        &root,
        reg,
        args.bool("dry_run"),
    );
    if changed(&result) {
        session.invalidate(touched_files(&result));
    }
    Ok(ToolOutput::Text(captured(agent::rename_symbol(&result))))
}

fn replace(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let content = with_trailing_newline(args.str("content"));
    let (root, reg) = (session.root.clone(), session.reg);
    let result = replace_symbol(
        session.index()?,
        args.str("target"),
        &content,
        &root,
        reg,
        args.bool("dry_run"),
    );
    if changed(&result) {
        session.invalidate(touched_files(&result));
    }
    Ok(ToolOutput::Text(captured(agent::replace_symbol(&result))))
}

fn refresh(session: &mut Session, args: &Args) -> Result<ToolOutput> {
    let (index, result) = crate::refresh::refresh(
        &session.root,
        session.reg,
        args.bool("full"),
        args.bool("keep_transactions"),
    )?;
    let out = json!({
        "ok": true,
        "status": "ok",
        "symbols": index.symbols.len(),
        "files": index.files.len(),
        "transactions_cleared": result.transactions_cleared,
    });
    session.replace_index(index);
    Ok(ToolOutput::Json(out))
}

fn undo(session: &mut Session) -> Result<ToolOutput> {
    let result = undo_last(&session.root);
    if result.status == "undone" {
        session.invalidate(result.files_restored.iter().cloned().collect());
    }
    Ok(ToolOutput::Json(json!({
        "ok": result.status != "error",
        "status": result.status,
        "transaction_id": result.transaction_id,
        "op": result.op,
        "subject": result.subject,
        "files_restored": result.files_restored,
        "files_skipped": result.files_skipped,
        "error_code": result.error_code,
        "message": result.message,
    })))
}
