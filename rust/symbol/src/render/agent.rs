//! Agent-format text for the writes (`--agent` and the MCP write tools) —
//! ports of the `_render_*_agent` / `_render_error` helpers in commands/.
//! Each returns exactly what the Python helper prints. Results arrive as
//! the write engines' JSON; absent fields read as the Python dataclass
//! defaults.

use serde_json::Value;

/// Collects `print()` lines.
#[derive(Default)]
struct Out(String);

impl Out {
    fn line(&mut self, text: impl AsRef<str>) {
        self.0.push_str(text.as_ref());
        self.0.push('\n');
    }

    fn blank(&mut self) {
        self.0.push('\n');
    }

    /// `print(text, end="" if text.endswith("\n") else "\n")`
    fn block(&mut self, text: &str) {
        self.0.push_str(text);
        if !text.ends_with('\n') {
            self.0.push('\n');
        }
    }

    fn diff(&mut self, diff: &str) {
        self.line("--- DIFF ---");
        self.block(diff);
        self.line("--- END ---");
    }

    fn finish(self) -> String {
        self.0
    }
}

/// `f"{value}"` for a JSON value: None/True/False spelled the Python way.
pub fn py(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn s<'a>(result: &'a Value, key: &str) -> &'a str {
    result[key].as_str().unwrap_or("")
}

fn int(result: &Value, key: &str) -> i64 {
    result[key].as_i64().unwrap_or(0)
}

fn list<'a>(result: &'a Value, key: &str) -> &'a [Value] {
    result[key].as_array().map(Vec::as_slice).unwrap_or(&[])
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn pair(value: &Value, i: usize) -> String {
    py(&value[i])
}

/// commands/patch.py `_render_error(code, message, agent=True)`.
pub fn patch_error(code: &Value, message: &Value) -> String {
    let mut out = Out::default();
    out.line("status: error");
    out.line(format!("error_code: {}", py(code)));
    out.line(format!("message: {}", py(message)));
    out.finish()
}

/// commands/patch.py `_render_needs_confirmation_agent`.
pub fn patch_needs_read(
    file_rel: &str,
    line_range: (i64, i64),
    byte_range: (usize, usize),
    current: &str,
) -> String {
    let mut out = Out::default();
    out.line("status: needs_read_confirmation");
    out.line(format!("file: {file_rel}"));
    out.line(format!(
        "range: lines {}-{} (bytes {}-{})",
        line_range.0, line_range.1, byte_range.0, byte_range.1
    ));
    out.line("reason: no cached read covers this range.");
    out.blank();
    out.line("--- CURRENT CONTENT ---");
    out.block(current);
    out.line("--- END ---");
    out.blank();
    out.line("Next: inspect the bytes above, then retry the same Patch call. The displayed");
    out.line("range has been recorded as seen — no separate Read needed. If the patch is");
    out.line("now wrong (different bytes than expected), revise content before retrying.");
    out.finish()
}

/// commands/patch.py `_render_result_agent`.
pub fn patch_result(result: &Value) -> String {
    let status = s(result, "status");
    let verb = if status == "dry_run" {
        "would apply"
    } else {
        "applied"
    };
    let mut out = Out::default();
    out.line(format!("status: {status}"));
    out.line(format!("file: {}", s(result, "file_rel")));
    out.line(format!(
        "{verb}: -{} +{} lines",
        int(result, "lines_removed"),
        int(result, "lines_added")
    ));
    if status == "applied" {
        out.line("undo: git checkout -- <file>  # revert uncommitted change");
    }
    out.blank();
    let diff = s(result, "diff");
    if !diff.is_empty() {
        out.diff(diff);
    }
    out.finish()
}

/// mcp/server.py `_format_multi_patch_result`.
pub fn multi_patch_result(result: &Value) -> String {
    let mut lines = vec![
        format!("status: {}", s(result, "status")),
        format!("file: {}", s(result, "file_rel")),
    ];
    if truthy(&result["error_code"]) {
        lines.push(format!("error_code: {}", py(&result["error_code"])));
    }
    if truthy(&result["message"]) {
        lines.push(format!("message: {}", py(&result["message"])));
    }
    let unconfirmed = list(result, "unconfirmed");
    if !unconfirmed.is_empty() {
        lines.push("unconfirmed:".into());
        for u in unconfirmed {
            lines.push(format!(
                "  - edit {}: range {}",
                py(&u["edit_idx"]),
                py(&u["range"])
            ));
        }
    }
    let per_edit = list(result, "per_edit");
    if !per_edit.is_empty() {
        lines.push(format!("edits: {}", per_edit.len()));
        for (i, e) in per_edit.iter().enumerate() {
            lines.push(format!(
                "  - {i}: {} lines {}-{} → {}-{}",
                py(&e["addressed_by"]),
                pair(&e["before_lines"], 0),
                pair(&e["before_lines"], 1),
                pair(&e["after_lines"], 0),
                pair(&e["after_lines"], 1),
            ));
        }
    }
    let diff = s(result, "diff");
    if !diff.is_empty() {
        lines.push("\n--- DIFF ---".into());
        lines.push(diff.trim_end().to_owned());
        lines.push("--- END ---".into());
    }
    lines.join("\n")
}

fn caller_line(c: &Value) -> String {
    let scope = if truthy(&c["source_path"]) {
        format!(" in {}", py(&c["source_path"]))
    } else {
        String::new()
    };
    format!(
        "  {}:{}{scope}  ({})",
        py(&c["file"]),
        py(&c["line"]),
        py(&c["kind"])
    )
}

/// commands/delete_symbol.py `_render_error(agent=True)` / `_render_agent`.
pub fn delete_symbol(result: &Value) -> String {
    let mut out = Out::default();
    let callers = list(result, "callers");
    if s(result, "status") == "error" {
        out.line("status: error");
        out.line(format!("error_code: {}", py(&result["error_code"])));
        out.line(format!("message: {}", py(&result["message"])));
        let candidates = list(result, "candidates");
        if !candidates.is_empty() {
            out.line("candidates:");
            for c in candidates {
                out.line(format!("  {}", py(c)));
            }
        }
        if !callers.is_empty() {
            out.line(format!("callers ({}):", callers.len()));
            for c in callers {
                out.line(caller_line(c));
            }
        }
        return out.finish();
    }
    let status = s(result, "status");
    let verb = if status == "dry_run" {
        "would delete"
    } else {
        "deleted"
    };
    let qualified_path = s(result, "qualified_path");
    out.line(format!("status: {status}"));
    out.line(format!("{verb}: {qualified_path}  ({})", s(result, "kind")));
    out.line(format!(
        "file: {}:{}-{}",
        s(result, "file_rel"),
        pair(&result["line_range"], 0),
        pair(&result["line_range"], 1)
    ));
    out.line(format!("lines_removed: {}", int(result, "lines_removed")));
    if !callers.is_empty() {
        out.blank();
        out.line(format!(
            "⚠️  {} reference(s) are now broken:",
            callers.len()
        ));
        for c in callers {
            out.line(caller_line(c));
        }
        let leaf = qualified_path.rsplit('.').next().unwrap_or(qualified_path);
        out.line(format!("Fix: symbol callers {leaf}"));
        out.line("Then: symbol patch <each file> to remove or rewrite each call site.");
    }
    let diff = s(result, "diff");
    if !diff.is_empty() {
        out.blank();
        out.diff(diff);
    }
    out.finish()
}

/// commands/insert_symbol.py `_render(format="rich", agent=True)`.
pub fn insert_symbol(result: &Value) -> String {
    let mut out = Out::default();
    let status = s(result, "status");
    if status == "error" {
        out.line(format!(
            "status: error\nerror_code: {}\nmessage: {}",
            py(&result["error_code"]),
            py(&result["message"])
        ));
        for c in list(result, "candidates") {
            out.line(format!("  candidate: {}", py(c)));
        }
        return out.finish();
    }
    let verb = if status == "dry_run" {
        "would insert"
    } else {
        "inserted"
    };
    out.line(format!("status: {status}"));
    out.line(format!(
        "{verb}: {} {}  ({})",
        s(result, "position"),
        s(result, "anchor_path"),
        s(result, "anchor_kind")
    ));
    out.line(format!(
        "file: {}:{}",
        s(result, "file_rel"),
        int(result, "insert_line")
    ));
    out.line(format!("lines_added: {}", int(result, "lines_added")));
    let diff = s(result, "diff");
    if !diff.is_empty() {
        out.blank();
        out.diff(diff);
    }
    out.finish()
}

/// `_render_error(agent=True)` shared by rename-symbol and replace-symbol.
fn candidate_error(result: &Value) -> String {
    let mut out = Out::default();
    out.line("status: error");
    out.line(format!("error_code: {}", py(&result["error_code"])));
    out.line(format!("message: {}", py(&result["message"])));
    for c in list(result, "candidates") {
        out.line(format!("  candidate: {}", py(c)));
    }
    out.finish()
}

/// commands/rename_symbol.py `_render_error` / `_render_agent`.
pub fn rename_symbol(result: &Value) -> String {
    let status = s(result, "status");
    if status == "error" {
        return candidate_error(result);
    }
    let verb = match status {
        "dry_run" => "would rename",
        "needs_review" => "needs review",
        _ => "renamed",
    };
    let unresolved = list(result, "unresolved");
    let skipped = list(result, "skipped_mismatch");
    let affected = list(result, "affected_interfaces");
    let tier = if !unresolved.is_empty() || !skipped.is_empty() {
        "ast"
    } else {
        "textual"
    };
    let qualified_path = s(result, "qualified_path");
    let leaf = qualified_path.rsplit('.').next().unwrap_or(qualified_path);
    let mut out = Out::default();
    out.line(format!("status: {status}"));
    out.line(format!(
        "{verb}: {qualified_path} → {}",
        s(result, "new_qualified_path")
    ));
    out.line(format!("files_changed: {}", int(result, "files_changed")));
    out.line(format!("refs_updated: {}", int(result, "refs_updated")));
    out.line(format!("tier: {tier}"));
    out.blank();
    for f in list(result, "per_file") {
        out.line(format!(
            "  {}  ({} refs)",
            py(&f["file"]),
            py(&f["refs_updated"])
        ));
    }
    if !skipped.is_empty() {
        out.blank();
        out.line(format!(
            "skipped {} site(s) — receiver resolved to a different declaration:",
            skipped.len()
        ));
        for site in skipped {
            out.line(format!(
                "  {}:{}:{}  `{}.{leaf}`  → {}",
                py(&site["file"]),
                py(&site["line"]),
                site["col"].as_i64().unwrap_or(0) + 1,
                py(&site["receiver_source"]),
                py(&site["resolved_to_qpath"])
            ));
        }
    }
    if !unresolved.is_empty() {
        out.blank();
        out.line(format!(
            "unresolved {} site(s) — review manually:",
            unresolved.len()
        ));
        for site in unresolved {
            out.line(format!(
                "  {}:{}:{}  `{}.{leaf}`  ({})",
                py(&site["file"]),
                py(&site["line"]),
                site["col"].as_i64().unwrap_or(0) + 1,
                py(&site["receiver_source"]),
                py(&site["why"])
            ));
        }
    }
    if !affected.is_empty() {
        out.blank();
        out.line(format!(
            "affects {} interface contract(s) — the rename target also implements these; \
             renaming will leave them unsatisfied unless you rename the contract method too:",
            affected.len()
        ));
        for a in affected {
            out.line(format!(
                "  {}  ({}:{})",
                py(&a["method_qpath"]),
                py(&a["file"]),
                py(&a["line"])
            ));
        }
    }
    if status == "applied" {
        out.blank();
        out.line("undo: symbol undo");
    }
    out.finish()
}

/// commands/replace_symbol.py `_render_error` / `_render_agent`.
pub fn replace_symbol(result: &Value) -> String {
    let status = s(result, "status");
    if status == "error" {
        return candidate_error(result);
    }
    let verb = if status == "dry_run" {
        "would replace"
    } else {
        "replaced"
    };
    let name_changed = result["name_changed"].as_bool().unwrap_or(false);
    let mut out = Out::default();
    out.line(format!("status: {status}"));
    if name_changed {
        out.line(format!(
            "{verb}: {} → {}",
            s(result, "qualified_path"),
            s(result, "new_qualified_path")
        ));
        out.line("name_changed: true");
    } else {
        out.line(format!("{verb}: {}", s(result, "qualified_path")));
        out.line("name_changed: false");
    }
    out.line(format!("kind: {}", s(result, "kind")));
    if truthy(&result["new_signature"]) {
        out.line(format!("signature: {}", s(result, "new_signature")));
    }
    out.line(format!("file: {}", s(result, "declaring_file")));
    out.line(format!("files_changed: {}", int(result, "files_changed")));
    if name_changed {
        out.line(format!("refs_updated: {}", int(result, "refs_updated")));
    }
    let per_file = list(result, "per_file");
    if !per_file.is_empty() {
        out.blank();
        for f in per_file {
            let n = f["refs_updated"].as_i64().unwrap_or(0);
            let plural = if n != 1 { "s" } else { "" };
            out.line(format!("  {}  ({n} change{plural})", py(&f["file"])));
        }
    }
    for pr in list(result, "parameter_renames") {
        out.line(format!(
            "parameter: {} → {}  ({} kwarg site(s), {} skipped, {} unresolved)",
            py(&pr["old"]),
            py(&pr["new"]),
            py(&pr["refs_updated"]),
            py(&pr["skipped_mismatch"]),
            py(&pr["unresolved"])
        ));
    }
    if result["signature_changed"].as_bool().unwrap_or(false) {
        let params = |key: &str| {
            let joined = list(result, key)
                .iter()
                .map(py)
                .collect::<Vec<_>>()
                .join(", ");
            if joined.is_empty() {
                "none".to_owned()
            } else {
                joined
            }
        };
        out.line(format!(
            "signature_changed: true  (params {} → {}; kwarg sites NOT updated)",
            params("old_parameters"),
            params("new_parameters")
        ));
    }
    if status == "applied" {
        out.blank();
        out.line("undo: git reset --hard HEAD~1");
    }
    out.finish()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn patch_error_spells_none_like_python() {
        assert_eq!(
            patch_error(&json!("not_found"), &Value::Null),
            "status: error\nerror_code: not_found\nmessage: None\n"
        );
    }

    #[test]
    fn multi_patch_lists_unconfirmed_edits() {
        let text = multi_patch_result(&json!({
            "status": "needs_read_confirmation",
            "file_rel": "a.py",
            "message": "1 edit(s) target ranges not in the read cache",
            "unconfirmed": [{"edit_idx": 0, "range": "3-4"}],
            "per_edit": [],
            "diff": "",
        }));
        assert_eq!(
            text,
            "status: needs_read_confirmation\nfile: a.py\n\
             message: 1 edit(s) target ranges not in the read cache\n\
             unconfirmed:\n  - edit 0: range 3-4"
        );
    }

    #[test]
    fn rename_without_sites_ends_with_blank_line() {
        let text = rename_symbol(&json!({
            "status": "dry_run",
            "qualified_path": "m.f",
            "new_qualified_path": "m.g",
            "files_changed": 1,
            "refs_updated": 2,
            "per_file": [],
        }));
        assert_eq!(
            text,
            "status: dry_run\nwould rename: m.f → m.g\nfiles_changed: 1\nrefs_updated: 2\ntier: textual\n\n"
        );
    }
}
