//! Port of writes/rename_symbol.py + writes/rename/renamer.py.
//!
//! Engine routing mirrors Python: supported Python and Go symbol kinds go
//! through their v2 receiver-aware daemon analysis. Unsupported kinds take
//! the tier-1 textual fallback. Results carry "engine": "v2" | "tier1".

use std::path::Path;

use crate::adapters::Registry;
use crate::index::Index;
use crate::writes::ops::{require_adapter, resolve_single as resolve_symbol_row};
use crate::writes::tx::{commit_edits, FileEdit};

const MEMBER_KINDS: [&str; 2] = ["method", "async_method"];
const BINDING_KINDS: [&str; 4] = ["function", "async_function", "class", "constant"];
const FIELD_KINDS: [&str; 2] = ["field", "attribute"];

// Go engine kinds (emitted by the go-scan adapter): `method` renames as a
// member (receiver-qualified), everything else as a module binding.
const GO_BINDING_KINDS: [&str; 4] = ["function", "type", "var", "const"];

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
            "message": "new-name must be a bare identifier (letters, digits, underscores). \
                        Use `symbol move-symbol` to change the module path.",
        });
    }
    let row = match resolve_symbol_row(index, qualified_path, reg) {
        Ok(r) => r,
        Err(e) => return e,
    };
    let declaring_file_rel = index.file_of(row).to_owned();
    let old_leaf = qualified_path
        .rsplit('.')
        .next()
        .unwrap_or(qualified_path)
        .to_owned();
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
            "message": format!("{} already exists", crate::pyjson::repr_str(&new_qpath)),
        });
    }

    let kind = index.kind_of(row).to_owned();
    let language = index
        .language_of_file(&declaring_file_rel)
        .unwrap_or("")
        .to_owned();
    let member = MEMBER_KINDS.contains(&kind.as_str());
    let field = FIELD_KINDS.contains(&kind.as_str());
    let py_engine =
        language == "python" && (member || field || BINDING_KINDS.contains(&kind.as_str()));

    let go_member = kind == "method";
    let go_engine = language == "go" && (go_member || GO_BINDING_KINDS.contains(&kind.as_str()));

    if py_engine {
        // Member and field both resolve the owner class (prefix minus leaf);
        // module bindings resolve the module qpath — same string, different
        // daemon param, keyed by the method name.
        let method = if member {
            "rename_member"
        } else if field {
            "rename_field"
        } else {
            "rename_module_binding"
        };
        rename_via_engine(
            index,
            qualified_path,
            &new_qpath,
            &old_leaf,
            new_name,
            row,
            method,
            &declaring_file_rel,
            prefix,
            project_root,
            reg,
            dry_run,
        )
    } else if go_engine {
        rename_via_go_engine(
            index,
            qualified_path,
            &new_qpath,
            &old_leaf,
            new_name,
            row,
            go_member,
            &declaring_file_rel,
            prefix,
            project_root,
            reg,
            dry_run,
        )
    } else {
        rename_tier1(
            index,
            qualified_path,
            &new_qpath,
            &old_leaf,
            new_name,
            &declaring_file_rel,
            project_root,
            dry_run,
        )
    }
}

// ---------------------------------------------------------- v2 engine path

#[allow(clippy::too_many_arguments)]
fn rename_via_engine(
    index: &mut Index,
    qualified_path: &str,
    new_qpath: &str,
    old_leaf: &str,
    new_name: &str,
    row: i64,
    method: &str,
    declaring_file_rel: &str,
    prefix: &str,
    project_root: &Path,
    reg: &Registry,
    dry_run: bool,
) -> serde_json::Value {
    let adapter = match require_adapter(reg, "python") {
        Ok(adapter) => adapter,
        Err(e) => return e,
    };

    let owner_qpath = prefix.trim_end_matches('.').to_owned();
    let decl_byte_range = index.byte_range_of(row);

    // _candidate_files: declaring + in-body refs + module-level import aliases
    // (+ imports that may name the target module, for module bindings).
    let module_qpath = (method == "rename_module_binding").then_some(owner_qpath.as_str());
    let mut candidates: std::collections::BTreeSet<String> =
        std::collections::BTreeSet::from([declaring_file_rel.to_owned()]);
    for (src_row, _line, _kind) in index.callers_of(old_leaf) {
        candidates.insert(index.file_of(src_row).to_owned());
    }
    for file_id in 0..index.files.len() as i64 {
        if index.imports_for(file_id).iter().any(|(local, source, _)| {
            *local == old_leaf || module_qpath.is_some_and(|m| may_import_from(source, m))
        }) {
            candidates.insert(index.files[file_id as usize].clone());
        }
    }

    let mut rewrites_out: Vec<serde_json::Value> = vec![];
    let mut skipped_out: Vec<serde_json::Value> = vec![];
    let mut unresolved_out: Vec<serde_json::Value> = vec![];
    let mut edits: Vec<FileEdit> = vec![];
    let mut per_file: Vec<serde_json::Value> = vec![];

    for rel in &candidates {
        // Engine analyzes python files only; skip other languages in the
        // candidate set exactly like _adapter_for's silent skip.
        if index.language_of_file(rel) != Some("python") {
            continue;
        }
        let abs = project_root.join(rel);
        let Ok(source) = std::fs::read(&abs) else {
            continue;
        };
        let is_declaring = rel == declaring_file_rel;
        let analysis = match adapter.rename_analysis(
            method,
            &abs,
            old_leaf,
            qualified_path,
            &owner_qpath,
            is_declaring,
            is_declaring.then_some(decl_byte_range),
            new_name,
            index,
        ) {
            Ok(a) => a,
            Err(e) => {
                return serde_json::json!({
                    "status": "error", "error_code": "adapter_failed",
                    "message": format!("rename analysis failed for {rel}: {e}"),
                })
            }
        };

        for r in &analysis.rewrites {
            rewrites_out.push(serde_json::json!({
                "file": rel, "line": r.line, "col": r.col,
                "receiver_source": r.receiver_source,
                "resolved_to_qpath": qualified_path,
            }));
        }
        for s in &analysis.skipped_mismatch {
            skipped_out.push(serde_json::json!({
                "file": rel, "line": s.line, "col": s.col,
                "receiver_source": s.receiver_source,
                "resolved_to_qpath": s.resolved_to_qpath,
            }));
        }
        for u in &analysis.unresolved {
            unresolved_out.push(serde_json::json!({
                "file": rel, "line": u.line, "col": u.col,
                "receiver_source": u.receiver_source, "why": u.why,
            }));
        }

        if !analysis.rewrites.is_empty() {
            let mut buf = source.clone();
            let mut sorted = analysis.rewrites.clone();
            sorted.sort_by_key(|r| std::cmp::Reverse(r.byte_start));
            for r in &sorted {
                if r.byte_end <= buf.len() && r.byte_start <= r.byte_end {
                    buf.splice(r.byte_start..r.byte_end, r.new_text.bytes());
                }
            }
            per_file.push(serde_json::json!({
                "file": rel, "refs_updated": analysis.rewrites.len(),
            }));
            edits.push(FileEdit {
                file_abs: abs,
                file_rel: rel.clone(),
                new_content: buf,
            });
        }
    }

    if edits.is_empty() {
        let review = !unresolved_out.is_empty() || !skipped_out.is_empty();
        return serde_json::json!({
            "status": if review { "needs_review" } else { "error" },
            "engine": "v2",
            "error_code": if review { serde_json::Value::Null } else { "nothing_to_rename".into() },
            "qualified_path": qualified_path,
            "new_qualified_path": new_qpath,
            "declaring_file": declaring_file_rel,
            "skipped_mismatch": skipped_out,
            "unresolved": unresolved_out,
            "message": engine_message(0, 0, &unresolved_out, &skipped_out, &[], old_leaf),
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
    let total: i64 = per_file
        .iter()
        .map(|f| f["refs_updated"].as_i64().unwrap_or(0))
        .sum();
    serde_json::json!({
        "status": if dry_run { "dry_run" } else { "applied" },
        "engine": "v2",
        "qualified_path": qualified_path,
        "new_qualified_path": new_qpath,
        "declaring_file": declaring_file_rel,
        "files_changed": edits.len(),
        "refs_updated": total,
        "per_file": per_file,
        "rewrites": rewrites_out,
        "skipped_mismatch": skipped_out,
        "unresolved": unresolved_out,
        "transaction_id": tx.transaction_id,
        "message": engine_message(edits.len(), total, &unresolved_out, &skipped_out, &[], old_leaf),
    })
}

/// renamer._format_message: the engine's one-paragraph summary.
fn engine_message(
    files_changed: usize,
    refs_updated: i64,
    unresolved: &[serde_json::Value],
    skipped: &[serde_json::Value],
    affected: &[serde_json::Value],
    leaf: &str,
) -> String {
    let s = |v: &serde_json::Value| v.as_str().unwrap_or("").to_owned();
    let col = |v: &serde_json::Value| v["col"].as_i64().unwrap_or(0) + 1;
    let mut parts = vec![if refs_updated != 0 {
        format!("renamed {refs_updated} ref(s) across {files_changed} file(s)")
    } else {
        format!(
            "no confident rewrites for {}",
            crate::pyjson::repr_str(leaf)
        )
    }];
    if !unresolved.is_empty() {
        parts.push(format!(
            "{} site(s) need manual review — receiver type could not be resolved statically:",
            unresolved.len()
        ));
        for u in unresolved {
            parts.push(format!(
                "  - {}:{}:{}  `{}.{leaf}`  ({})",
                s(&u["file"]),
                u["line"],
                col(u),
                s(&u["receiver_source"]),
                s(&u["why"])
            ));
        }
    }
    if !skipped.is_empty() {
        parts.push(format!(
            "{} site(s) correctly skipped — receiver resolved to a different declaration:",
            skipped.len()
        ));
        for k in skipped {
            parts.push(format!(
                "  - {}:{}:{}  `{}.{leaf}`  → {}",
                s(&k["file"]),
                k["line"],
                col(k),
                s(&k["receiver_source"]),
                s(&k["resolved_to_qpath"])
            ));
        }
    }
    if !affected.is_empty() {
        parts.push(format!(
            "affects {} interface contract(s) — the rename target also implements these; \
             renaming will leave them unsatisfied unless you rename the contract method too:",
            affected.len()
        ));
        for a in affected {
            parts.push(format!(
                "  - {}  ({}:{})",
                s(&a["method_qpath"]),
                s(&a["file"]),
                a["line"]
            ));
        }
    }
    parts.join("\n")
}

/// renamer._may_import_from: the import source names `module_qpath` itself,
/// a relative spelling of it, or a parent package that may re-export it.
fn may_import_from(source: &str, module_qpath: &str) -> bool {
    !source.is_empty()
        && (source == module_qpath
            || module_qpath.ends_with(&format!(".{source}"))
            || module_qpath.starts_with(&format!("{source}.")))
}

// ---------------------------------------------------------- go v2 engine path

/// Go rename via the go-scan daemon: a single project-wide RPC (the daemon
/// loads the whole project through go/packages and resolves receiver types
/// with go/types), then splice the returned per-file rewrites. Unlike the
/// python path there is NO `index.save()` — the daemon never reads the
/// on-disk index; it walks the source tree itself.
#[allow(clippy::too_many_arguments)]
fn rename_via_go_engine(
    index: &mut Index,
    qualified_path: &str,
    new_qpath: &str,
    old_leaf: &str,
    new_name: &str,
    row: i64,
    member: bool,
    declaring_file_rel: &str,
    prefix: &str,
    project_root: &Path,
    reg: &Registry,
    dry_run: bool,
) -> serde_json::Value {
    let adapter = match require_adapter(reg, "go") {
        Ok(adapter) => adapter,
        Err(e) => return e,
    };

    let container_qpath = prefix.trim_end_matches('.').to_owned();
    let decl_byte_range = index.byte_range_of(row);

    let files = match adapter.go_rename(
        member,
        project_root,
        old_leaf,
        new_name,
        qualified_path,
        &container_qpath,
        declaring_file_rel,
        decl_byte_range,
    ) {
        Ok(f) => f,
        Err(e) => {
            return serde_json::json!({
                "status": "error", "error_code": "adapter_failed",
                "message": format!("go rename analysis failed: {e}"),
            })
        }
    };

    // Capability is never artificially discarded — the tier label is the
    // only cap. The daemon's go/types analysis is project-wide and strictly
    // more complete than any index-derived candidate heuristic, so EVERY
    // file it reports is applied. (The Python oracle mirrors this via the
    // adapter's pop_project_wide_analyses hook in renamer._run_per_file.)
    let mut rewrites_out: Vec<serde_json::Value> = vec![];
    let mut skipped_out: Vec<serde_json::Value> = vec![];
    let mut unresolved_out: Vec<serde_json::Value> = vec![];
    let mut edits: Vec<FileEdit> = vec![];
    let mut per_file: Vec<serde_json::Value> = vec![];

    // Deterministic iteration for stable output ordering.
    let mut rels: Vec<&String> = files.keys().collect();
    rels.sort();

    for rel in rels {
        let analysis = &files[rel];
        for r in &analysis.rewrites {
            rewrites_out.push(serde_json::json!({
                "file": rel, "line": r.line, "col": r.col,
                "receiver_source": r.receiver_source,
                "resolved_to_qpath": qualified_path,
            }));
        }
        for s in &analysis.skipped_mismatch {
            skipped_out.push(serde_json::json!({
                "file": rel, "line": s.line, "col": s.col,
                "receiver_source": s.receiver_source,
                "resolved_to_qpath": s.resolved_to_qpath,
            }));
        }
        for u in &analysis.unresolved {
            unresolved_out.push(serde_json::json!({
                "file": rel, "line": u.line, "col": u.col,
                "receiver_source": u.receiver_source, "why": u.why,
            }));
        }

        if !analysis.rewrites.is_empty() {
            let abs = project_root.join(rel);
            let Ok(source) = std::fs::read(&abs) else {
                continue;
            };
            let mut buf = source;
            let mut sorted = analysis.rewrites.clone();
            sorted.sort_by_key(|r| std::cmp::Reverse(r.byte_start));
            for r in &sorted {
                if r.byte_end <= buf.len() && r.byte_start <= r.byte_end {
                    buf.splice(r.byte_start..r.byte_end, r.new_text.bytes());
                }
            }
            per_file.push(serde_json::json!({
                "file": rel, "refs_updated": analysis.rewrites.len(),
            }));
            edits.push(FileEdit {
                file_abs: abs,
                file_rel: rel.clone(),
                new_content: buf,
            });
        }
    }

    // Interface impacts ride the declaring file's entry only; dedupe by
    // interface_qpath.
    let mut affected_out: Vec<serde_json::Value> = vec![];
    if let Some(decl_entry) = files.get(declaring_file_rel) {
        let mut seen: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
        for a in &decl_entry.affected_interfaces {
            if a.interface_qpath.is_empty() || !seen.insert(a.interface_qpath.clone()) {
                continue;
            }
            affected_out.push(serde_json::json!({
                "interface_qpath": a.interface_qpath,
                "method_qpath": a.method_qpath,
                "file": a.file,
                "line": a.line,
            }));
        }
    }

    if edits.is_empty() {
        let review =
            !unresolved_out.is_empty() || !skipped_out.is_empty() || !affected_out.is_empty();
        return serde_json::json!({
            "status": if review { "needs_review" } else { "error" },
            "engine": "v2",
            "error_code": if review { serde_json::Value::Null } else { "nothing_to_rename".into() },
            "qualified_path": qualified_path,
            "new_qualified_path": new_qpath,
            "declaring_file": declaring_file_rel,
            "skipped_mismatch": skipped_out,
            "unresolved": unresolved_out,
            "affected_interfaces": affected_out,
            "message": engine_message(0, 0, &unresolved_out, &skipped_out, &affected_out, old_leaf),
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
    let total: i64 = per_file
        .iter()
        .map(|f| f["refs_updated"].as_i64().unwrap_or(0))
        .sum();
    serde_json::json!({
        "status": if dry_run { "dry_run" } else { "applied" },
        "engine": "v2",
        "qualified_path": qualified_path,
        "new_qualified_path": new_qpath,
        "declaring_file": declaring_file_rel,
        "files_changed": edits.len(),
        "refs_updated": total,
        "per_file": per_file,
        "rewrites": rewrites_out,
        "skipped_mismatch": skipped_out,
        "unresolved": unresolved_out,
        "affected_interfaces": affected_out,
        "transaction_id": tx.transaction_id,
        "message": engine_message(edits.len(), total, &unresolved_out, &skipped_out, &affected_out, old_leaf),
    })
}

// ---------------------------------------------------------- tier-1 path

#[allow(clippy::too_many_arguments)]
fn rename_tier1(
    index: &mut Index,
    qualified_path: &str,
    new_qpath: &str,
    old_leaf: &str,
    new_name: &str,
    declaring_file_rel: &str,
    project_root: &Path,
    dry_run: bool,
) -> serde_json::Value {
    let mut ref_files: std::collections::BTreeSet<String> =
        std::collections::BTreeSet::from([declaring_file_rel.to_owned()]);
    for (src_row, _line, _kind) in index.callers_of(old_leaf) {
        ref_files.insert(index.file_of(src_row).to_owned());
    }

    let pattern = regex::Regex::new(&format!(r"\b{}\b", regex::escape(old_leaf))).unwrap();
    let mut edits: Vec<FileEdit> = vec![];
    let mut per_file: Vec<serde_json::Value> = vec![];
    for rel in &ref_files {
        let abs = project_root.join(rel);
        let Ok(source) = std::fs::read(&abs) else {
            continue;
        };
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
                "no occurrences of {} found (even in the declaring file — this is unusual)",
                crate::pyjson::repr_str(old_leaf)
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

    let total: i64 = per_file
        .iter()
        .map(|f| f["refs_updated"].as_i64().unwrap_or(0))
        .sum();
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
