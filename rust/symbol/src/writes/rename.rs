//! Port of writes/rename_symbol.py — TIER-1 TEXTUAL rename: resolve the
//! declaration via the index, word-boundary regex rewrite across every file
//! the ref table implicates, atomic multi-file commit.
//!
//! DIVERGENCE (documented): Python routes python/go method/function/class
//! kinds through the v2 SymbolRenamer engine (adapter-resolved, receiver-
//! aware). That engine's per-file RPC surface isn't wired here yet, so ALL
//! kinds take the tier-1 path; the result carries "engine": "tier1" so
//! callers can tell.

use std::path::Path;

use crate::adapters::Registry;
use crate::index::Index;
use crate::writes::ops::resolve_single as resolve_symbol_row;
use crate::writes::tx::{commit_edits, FileEdit};

pub fn rename_symbol(
    index: &mut Index,
    qualified_path: &str,
    new_name: &str,
    project_root: &Path,
    reg: &Registry,
    dry_run: bool,
) -> serde_json::Value {
    static IDENT_RE: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"^[A-Za-z_][A-Za-z_0-9]*$").unwrap());

    if !IDENT_RE.is_match(new_name) {
        return serde_json::json!({
            "status": "error", "error_code": "invalid_argument",
            "message": "new-name must be a bare identifier (letters, digits, underscores).",
        });
    }
    let row = match resolve_symbol_row(index, qualified_path, reg) {
        Ok(r) => r,
        Err(e) => return e,
    };
    let declaring_file_rel = index.file_of(row).to_owned();
    let old_leaf = qualified_path.rsplit('.').next().unwrap_or(qualified_path).to_owned();
    if old_leaf == new_name {
        return serde_json::json!({
            "status": "error", "error_code": "invalid_argument",
            "message": "new-name is identical to current name",
        });
    }
    let prefix = &qualified_path[..qualified_path.len() - old_leaf.len()];
    let new_qpath = if prefix.is_empty() {
        new_name.to_owned()
    } else {
        format!("{prefix}{new_name}")
    };
    if index.by_path.contains_key(&new_qpath) {
        return serde_json::json!({
            "status": "error", "error_code": "name_collision",
            "message": format!("{new_qpath:?} already exists"),
        });
    }

    let mut ref_files: std::collections::BTreeSet<String> =
        std::collections::BTreeSet::from([declaring_file_rel.clone()]);
    for (src_row, _line, _kind) in index.callers_of(&old_leaf) {
        ref_files.insert(index.file_of(src_row).to_owned());
    }

    let pattern = regex::Regex::new(&format!(r"\b{}\b", regex::escape(&old_leaf))).unwrap();
    let mut edits: Vec<FileEdit> = vec![];
    let mut per_file: Vec<serde_json::Value> = vec![];
    for rel in &ref_files {
        let abs = project_root.join(rel);
        let Ok(source) = std::fs::read(&abs) else { continue };
        let text = String::from_utf8_lossy(&source).into_owned();
        let count = pattern.find_iter(&text).count();
        if count == 0 {
            continue;
        }
        let new_text = pattern.replace_all(&text, new_name).into_owned();
        edits.push(FileEdit {
            file_abs: abs,
            file_rel: rel.clone(),
            new_content: new_text.into_bytes(),
        });
        per_file.push(serde_json::json!({ "file": rel, "refs_updated": count }));
    }

    if edits.is_empty() {
        return serde_json::json!({
            "status": "error", "error_code": "nothing_to_rename",
            "message": format!(
                "no occurrences of {old_leaf:?} found (even in the declaring file — this is unusual)"
            ),
        });
    }

    let subject = format!("{qualified_path} → {new_name}");
    let tx = commit_edits(&edits, project_root, "rename-symbol", &subject, dry_run);
    if tx.status == "error" {
        return serde_json::json!({
            "status": "error", "error_code": tx.error_code, "message": tx.message,
            "qualified_path": qualified_path, "new_qualified_path": new_qpath,
        });
    }

    let total: i64 = per_file.iter().map(|f| f["refs_updated"].as_i64().unwrap_or(0)).sum();
    serde_json::json!({
        "status": if dry_run { "dry_run" } else { "applied" },
        "engine": "tier1",
        "qualified_path": qualified_path,
        "new_qualified_path": new_qpath,
        "declaring_file": declaring_file_rel,
        "files_changed": edits.len(),
        "refs_updated": total,
        "per_file": per_file,
        "transaction_id": tx.transaction_id,
    })
}
