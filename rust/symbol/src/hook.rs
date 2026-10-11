//! Port of commands/hook.py — the Claude Code hook entry point.
//!
//! PreToolUse (Read / Grep / Edit / Write) on an indexed project nudges
//! toward the symbol tools via `additionalContext`, or with `enforce` blocks
//! the call (exit 2, reason on stderr). PostToolUse (Edit / Write) refreshes
//! the edited file in the index from a detached child. Without an index both
//! are no-ops; neither ever builds one. The PreToolUse path answers from the
//! index alone and never starts a language daemon.

use std::collections::HashSet;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use serde_json::Value;

use crate::adapters::Registry;
use crate::index::Index;
use crate::reads::{search_rows, SearchOpts};

/// Lines at or below which a file is too small for the nudge to pay off.
const SMALL_FILE_LINES: usize = 80;

/// Process exit code for one hook invocation; output is already written.
pub fn run(enforce: bool) -> i32 {
    let mut raw = String::new();
    if std::io::stdin().read_to_string(&mut raw).is_err() {
        return 0;
    }
    let Ok(payload) = serde_json::from_str::<Value>(&raw) else {
        return 0;
    };
    if !payload.is_object() {
        return 0;
    }
    let event = payload["hook_event_name"].as_str().unwrap_or("");
    let cwd = match payload["cwd"].as_str().filter(|c| !c.is_empty()) {
        Some(c) => PathBuf::from(c),
        None => std::env::current_dir().unwrap_or_default(),
    };
    let Some(project_root) = find_project_root(&cwd) else {
        return 0;
    };
    let reg = crate::adapters::default_registry(&project_root);
    match event {
        "PreToolUse" => {
            let Some(message) = pre_tool_message(&project_root, &payload, reg) else {
                return 0;
            };
            if enforce {
                eprintln!("{message}");
                return 2;
            }
            let out = serde_json::json!({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": message,
                }
            });
            print!("{}", crate::pyjson::dumps(&out));
            let _ = std::io::Write::flush(&mut std::io::stdout());
            0
        }
        "PostToolUse" => {
            post_tool_refresh(&project_root, &payload, reg);
            0
        }
        _ => 0,
    }
}

fn tool_input(payload: &Value) -> &Value {
    static EMPTY: Value = Value::Null;
    match &payload["tool_input"] {
        v @ Value::Object(_) => v,
        _ => &EMPTY,
    }
}

fn input_str<'a>(input: &'a Value, key: &str) -> &'a str {
    input[key].as_str().unwrap_or("")
}

// ---------------------------------------------------------- PreToolUse

pub fn pre_tool_message(project_root: &Path, payload: &Value, reg: &Registry) -> Option<String> {
    let tool = payload["tool_name"].as_str().unwrap_or("");
    let input = tool_input(payload);
    if !matches!(tool, "Read" | "Grep" | "Edit" | "Write") {
        return None;
    }
    // File-bearing tools skip anything no adapter handles (md, json,
    // settings); linguist sniffing decides, not the extension.
    if tool != "Grep" {
        let file_path = input_str(input, "file_path").trim();
        if file_path.is_empty() {
            return None;
        }
        let path = Path::new(file_path);
        if !path.is_file() || reg.language_for_file(path).is_none() {
            return None;
        }
    }
    let index = Index::load(project_root)?;
    match tool {
        "Read" => read_suggestion(&index, project_root, input, reg),
        "Grep" => grep_suggestion(&index, input),
        "Edit" => edit_suggestion(&index, project_root, input, reg),
        _ => write_suggestion(&index, project_root, input, reg),
    }
}

fn read_suggestion(index: &Index, root: &Path, input: &Value, reg: &Registry) -> Option<String> {
    let rel = to_rel(root, input_str(input, "file_path"))?;
    let lang = detect_language(index, root, &rel, reg);
    if too_small(&root.join(&rel)) {
        return None;
    }
    Some(format!(
        "symbol: \"{rel}\" is indexed ({lang}). Read returns raw file lines — \
         SymbolBody returns the same range with structural awareness (used imports, \
         refs, declared kind) at the same or lower token cost. Use:\n  \
         • SymbolOutline(target=\"{rel}\") — file structure, no bodies\n  \
         • SymbolBody(target=\"<qualified.path>\") — one named symbol + imports + refs\n  \
         • SymbolBody(target=\"{rel}:START-END\") — arbitrary line range with refs\n\
         Read only when you genuinely need the unstructured file content (top-level \
         comments, module-level constants, or non-code regions)."
    ))
}

fn grep_suggestion(index: &Index, input: &Value) -> Option<String> {
    let pattern = input_str(input, "pattern").trim();
    if pattern.is_empty() {
        return None;
    }
    let lookup = |regex: bool| {
        let opts = SearchOpts {
            regex,
            limit: 5,
            ..Default::default()
        };
        search_rows(index, &[pattern.to_owned()], &opts).unwrap_or_default()
    };
    let mut hits = lookup(false);
    if hits.is_empty() {
        hits = lookup(true);
    }
    let sample = index.path_of(*hits.first()?);
    let leaf = sample.rsplit('.').next().unwrap_or(sample);
    let n = hits.len();
    let more = if n >= 5 { "+" } else { "" };
    Some(format!(
        "symbol: \"{pattern}\" is the name of {n}{more} indexed declaration(s) \
         (e.g. {sample}). Grep returns raw line matches mixed with comments, \
         strings, and unrelated occurrences — typically 5-10× the noise of a \
         symbol-aware lookup. Use:\n  \
         • SearchSymbol(patterns=[\"{pattern}\"]) — declarations only\n  \
         • SymbolCallers(name=\"{leaf}\") — every containing symbol that references it\n  \
         • SymbolBody(target=\"{sample}\") — the body, used imports, refs\n\
         Re-issue Grep only for non-identifier text (TODOs, doc hunts, regex)."
    ))
}

/// Only an overwrite of an existing indexed file; creating files is what
/// Write is for.
fn write_suggestion(index: &Index, root: &Path, input: &Value, reg: &Registry) -> Option<String> {
    let rel = to_rel(root, input_str(input, "file_path"))?;
    if !root.join(&rel).exists() {
        return None;
    }
    let lang = detect_language(index, root, &rel, reg);
    if too_small(&root.join(&rel)) {
        return None;
    }
    Some(format!(
        "symbol: \"{rel}\" already exists and is indexed ({lang}). Write would \
         overwrite it wholesale, losing the per-symbol structural edit guarantees. \
         For changes to an existing indexed file, use:\n  \
         • Patch(file=..., range=A-B, content=...) — byte-range edit, atomic\n  \
         • ReplaceSymbol(qualified_path=..., content=...) — whole symbol; parse-validated\n  \
         • InsertSymbol(anchor=..., position=..., content=...) — structural insert\n\
         Write is correct only for *new* files (it's a no-op nudge in that case)."
    ))
}

fn edit_suggestion(index: &Index, root: &Path, input: &Value, reg: &Registry) -> Option<String> {
    let rel = to_rel(root, input_str(input, "file_path"))?;
    let lang = detect_language(index, root, &rel, reg);
    if too_small(&root.join(&rel)) {
        return None;
    }
    Some(format!(
        "symbol: \"{rel}\" is indexed ({lang}). Edit requires re-sending the existing \
         content for disambiguation (~200 extra tokens per call) and provides no \
         parse validation. The MCP write tools cover every case Edit covers:\n  \
         • Patch(file=..., range=A-B, content=...) — byte-range, no old_string round-trip (handles ALL non-symbol edits)\n  \
         • ReplaceSymbol(qualified_path=..., content=...) — whole symbol; parse-validated; rewrites callers if leaf renamed\n  \
         • InsertSymbol(anchor=..., position=before|after|start|end, content=...) — auto-indented, structural\n  \
         • DeleteSymbol(qualified_path=...) — refuses if callers exist\n  \
         • RenameSymbol(qualified_path=..., new_name=...) — atomic across files, transactional (Undo-able)\n\
         Use Edit only on files symbol does not index."
    ))
}

/// The index's language for the file, else linguist's, else "code".
fn detect_language(index: &Index, root: &Path, rel: &str, reg: &Registry) -> String {
    if let Some(lang) = index.language_of_file(rel) {
        return lang.to_owned();
    }
    reg.language_for_file(&root.join(rel))
        .unwrap_or_else(|| "code".to_owned())
}

/// At most SMALL_FILE_LINES lines (universal newlines, like a Python text
/// read). Unreadable or non-UTF-8 files count as not small.
fn too_small(path: &Path) -> bool {
    let Ok(text) = std::fs::read(path).map(String::from_utf8) else {
        return false;
    };
    let Ok(text) = text else {
        return false;
    };
    let mut lines = 0;
    let mut chars = text.chars().peekable();
    let mut open = false;
    while let Some(c) = chars.next() {
        open = true;
        if c == '\n' || c == '\r' {
            if c == '\r' && chars.peek() == Some(&'\n') {
                chars.next();
            }
            lines += 1;
            open = false;
            if lines > SMALL_FILE_LINES {
                return false;
            }
        }
    }
    lines + usize::from(open) <= SMALL_FILE_LINES
}

// ---------------------------------------------------------- PostToolUse

fn post_tool_refresh(project_root: &Path, payload: &Value, reg: &Registry) {
    let tool = payload["tool_name"].as_str().unwrap_or("");
    if !matches!(tool, "Edit" | "Write") {
        return;
    }
    let response = &payload["tool_response"];
    if response.is_object() && truthy(&response["error"]) {
        return;
    }
    let input = tool_input(payload);
    let file_path = input_str(input, "file_path").trim();
    if file_path.is_empty() {
        return;
    }
    // A deleted file can't be classified; refresh tombstones it.
    let path = Path::new(file_path);
    if path.exists() && reg.language_for_file(path).is_none() {
        return;
    }
    let Some(rel) = to_rel(project_root, file_path) else {
        return;
    };
    detached(|| refresh_one(project_root, &rel, reg));
}

fn refresh_one(project_root: &Path, rel: &str, reg: &Registry) {
    let Some(mut index) = Index::load(project_root) else {
        return;
    };
    let one = HashSet::from([rel.to_owned()]);
    let none = HashSet::new();
    let refreshed = if project_root.join(rel).exists() {
        index.refresh(&one, &none, reg)
    } else {
        index.refresh(&none, &one, reg)
    };
    if refreshed.is_ok() {
        let _ = index.save();
    }
}

/// Run `work` in a forked, session-detached child with stdio on /dev/null;
/// the parent returns at once so the agent isn't blocked. Synchronous where
/// fork is unavailable.
#[cfg(unix)]
fn detached(work: impl FnOnce()) {
    // SAFETY: the hook is single-threaded up to here, so the child may run
    // arbitrary code; it leaves through _exit and never returns.
    unsafe {
        match libc::fork() {
            0 => {
                libc::setsid();
                let devnull = libc::open(c"/dev/null".as_ptr(), libc::O_RDWR);
                if devnull >= 0 {
                    libc::dup2(devnull, 1);
                    libc::dup2(devnull, 2);
                }
                work();
                libc::_exit(0);
            }
            -1 => work(),
            _ => {}
        }
    }
}

#[cfg(not(unix))]
fn detached(work: impl FnOnce()) {
    work();
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

// ---------------------------------------------------------- paths

/// Nearest ancestor holding a `.symbol/` directory — the only marker, so
/// a stray pyproject.toml or .git elsewhere never activates the hook.
pub fn find_project_root(start: &Path) -> Option<PathBuf> {
    let mut p = resolve(start);
    while let Some(parent) = p.parent() {
        if p.join(".symbol").is_dir() {
            return Some(p);
        }
        p = parent.to_owned();
    }
    None
}

/// `Path.resolve()`: absolute, symlinks resolved as far as the path exists.
fn resolve(path: &Path) -> PathBuf {
    let abs = std::path::absolute(path).unwrap_or_else(|_| path.to_owned());
    let mut existing = abs.clone();
    let mut rest: Vec<std::ffi::OsString> = vec![];
    loop {
        if let Ok(real) = existing.canonicalize() {
            let mut out = real;
            for part in rest.iter().rev() {
                out.push(part);
            }
            return normalize(&out);
        }
        match (existing.file_name(), existing.parent()) {
            (Some(name), Some(parent)) => {
                rest.push(name.to_owned());
                existing = parent.to_owned();
            }
            _ => return normalize(&abs),
        }
    }
}

fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

fn to_rel(project_root: &Path, file_path: &str) -> Option<String> {
    let rel = resolve(Path::new(file_path))
        .strip_prefix(project_root)
        .ok()?
        .to_string_lossy()
        .into_owned();
    Some(if rel.is_empty() { ".".to_owned() } else { rel })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn small_file_threshold_counts_universal_newlines() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("f.py");
        std::fs::write(&file, "x\n".repeat(80)).unwrap();
        assert!(too_small(&file));
        std::fs::write(&file, "x\n".repeat(81)).unwrap();
        assert!(!too_small(&file));
        std::fs::write(&file, format!("{}x", "x\r".repeat(80))).unwrap();
        assert!(!too_small(&file));
        std::fs::write(&file, b"\xff\xfe").unwrap();
        assert!(!too_small(&file));
    }

    #[test]
    fn project_root_is_the_nearest_symbol_dir() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        std::fs::create_dir_all(root.join(".symbol")).unwrap();
        std::fs::create_dir_all(root.join("a/b")).unwrap();
        assert_eq!(find_project_root(&root.join("a/b")), Some(root.clone()));
        assert_eq!(
            to_rel(&root, &root.join("a/../a/b/x.py").to_string_lossy()),
            Some("a/b/x.py".to_owned())
        );
        assert_eq!(to_rel(&root, "/elsewhere/x.py"), None);
    }
}
