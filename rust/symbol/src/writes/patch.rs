//! Byte-range edit primitive: validation, read-cache preflight, splice,
//! atomic write, numbered unified diff.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::writes::read_cache::{CachedRead, ReadCache};

#[derive(Debug, Clone)]
pub struct PatchRequest {
    pub file_abs: PathBuf,
    pub file_rel: String,
    pub line_range: (i64, i64),
    pub byte_range: (usize, usize),
    pub content: Vec<u8>,
    pub force: bool,
}

#[derive(Serialize)]
pub struct PatchResult {
    pub status: &'static str, // applied | dry_run | error
    pub file_rel: String,
    pub before_range: (usize, usize),
    pub after_range: (usize, usize),
    pub new_line_range: (i64, i64),
    pub new_content: String,
    pub lines_removed: i64,
    pub lines_added: i64,
    pub diff: String,
    pub error_code: Option<String>,
    pub message: Option<String>,
}

impl PatchResult {
    fn error(file_rel: &str, code: &str, message: String) -> PatchResult {
        PatchResult {
            status: "error",
            file_rel: file_rel.to_owned(),
            before_range: (0, 0),
            after_range: (0, 0),
            new_line_range: (0, 0),
            new_content: String::new(),
            lines_removed: 0,
            lines_added: 0,
            diff: String::new(),
            error_code: Some(code.to_owned()),
            message: Some(message),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MultiPatchEdit {
    #[serde(default)]
    pub range: Option<String>,
    #[serde(default)]
    pub old: Option<String>,
    #[serde(default)]
    pub content: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct MultiPatchResult {
    pub status: &'static str,
    pub file_rel: String,
    pub diff: String,
    pub per_edit: Vec<Value>,
    pub unconfirmed: Vec<Value>,
    pub error_code: Option<String>,
    pub message: Option<String>,
}

impl MultiPatchResult {
    fn error(file_rel: &str, code: &str, message: String) -> Self {
        Self {
            status: "error",
            file_rel: file_rel.into(),
            diff: String::new(),
            per_edit: vec![],
            unconfirmed: vec![],
            error_code: Some(code.into()),
            message: Some(message),
        }
    }
}

#[derive(Debug)]
struct ResolvedMultiEdit {
    index: usize,
    start: usize,
    end: usize,
    content: Vec<u8>,
    addressed_by: &'static str,
    line_range: (i64, i64),
}

// ---------------------------------------------------------- ranges

pub fn parse_line_range(raw: &str) -> Result<(i64, i64), String> {
    static RANGE_RE: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"^(\d+)-(\d+)$").unwrap());
    let m = RANGE_RE.captures(raw.trim()).ok_or_else(|| {
        format!(
            "range must be 'A-B' with non-negative integers, got {}",
            crate::pystr::repr(raw)
        )
    })?;
    let start: i64 = m[1].parse().map_err(|e| format!("{e}"))?;
    let end: i64 = m[2].parse().map_err(|e| format!("{e}"))?;
    if start < 1 {
        return Err(format!("range start must be >= 1, got {start}"));
    }
    if end < start {
        return Err(format!("range end ({end}) must be >= start ({start})"));
    }
    Ok((start, end))
}

pub fn line_start_byte(data: &[u8], line: i64) -> usize {
    if line <= 1 {
        return 0;
    }
    let mut seen = 1;
    for (i, &b) in data.iter().enumerate() {
        if b == 0x0A {
            seen += 1;
            if seen == line {
                return i + 1;
            }
        }
    }
    data.len()
}

pub fn line_end_byte(data: &[u8], line: i64) -> usize {
    let mut seen = 0;
    for (i, &b) in data.iter().enumerate() {
        if b == 0x0A {
            seen += 1;
            if seen == line {
                return i + 1;
            }
        }
    }
    data.len()
}

pub fn count_lines(data: &[u8]) -> i64 {
    if data.is_empty() {
        return 0;
    }
    let mut count = data.iter().filter(|&&b| b == 0x0A).count() as i64;
    if data.last() != Some(&0x0A) {
        count += 1;
    }
    count
}

fn looks_binary(data: &[u8]) -> bool {
    memchr_nul(&data[..data.len().min(8192)])
}

fn memchr_nul(data: &[u8]) -> bool {
    data.contains(&0)
}

// ---------------------------------------------------------- validate

#[allow(clippy::result_large_err)]
pub fn validate_args(
    file: &str,
    raw_range: &str,
    content: &[u8],
    project_root: &Path,
    force: bool,
) -> Result<PatchRequest, PatchResult> {
    let input = Path::new(file);
    let candidate = if input.is_absolute() {
        input.to_owned()
    } else {
        project_root.join(input)
    };
    let file_abs = match candidate.canonicalize() {
        Ok(p) => p,
        Err(_) => {
            return Err(PatchResult::error(
                file,
                "file_not_found",
                format!("no such file: {file}"),
            ))
        }
    };
    if !file_abs.is_file() {
        return Err(PatchResult::error(
            file,
            "file_not_found",
            format!("not a regular file: {file}"),
        ));
    }
    let source = match std::fs::read(&file_abs) {
        Ok(s) => s,
        Err(e) => {
            return Err(PatchResult::error(
                file,
                "file_not_found",
                format!("cannot read {file}: {e}"),
            ))
        }
    };
    if looks_binary(&source) {
        return Err(PatchResult::error(
            file,
            "binary_file",
            format!("{file} appears to be binary"),
        ));
    }
    let line_range = match parse_line_range(raw_range) {
        Ok(r) => r,
        Err(e) => return Err(PatchResult::error(file, "invalid_argument", e)),
    };
    let total = count_lines(&source);
    if line_range.1 > total {
        return Err(PatchResult::error(
            file,
            "range_out_of_bounds",
            format!(
                "range end {} exceeds file length ({total} lines)",
                line_range.1
            ),
        ));
    }
    let byte_range = (
        line_start_byte(&source, line_range.0),
        line_end_byte(&source, line_range.1),
    );
    let file_rel = file_abs
        .strip_prefix(project_root)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| file_abs.to_string_lossy().into_owned());
    Ok(PatchRequest {
        file_abs,
        file_rel,
        line_range,
        byte_range,
        content: content.to_vec(),
        force,
    })
}

/// Preflight: force bypasses; otherwise a covering, mtime-fresh cache entry
/// is required. Returns Ok(()) to proceed, Err(msg) for needs_read_confirmation.
pub fn preflight(request: &PatchRequest, cache: &ReadCache) -> Result<(), String> {
    if request.force {
        return Ok(());
    }
    if let Some(covering) = cache.find_covering(&request.file_rel, request.byte_range) {
        if let Some(mt) = mtime_f64(&request.file_abs) {
            if mtime_matches(covering.served_mtime, mt) {
                return Ok(());
            }
        }
    }
    Err(format!(
        "range {}-{} of {} is not covered by a fresh read; read it first (symbol code) or pass --force",
        request.line_range.0, request.line_range.1, request.file_rel
    ))
}

// ---------------------------------------------------------- apply

pub fn apply_patch(
    request: &PatchRequest,
    cache: &ReadCache,
    dry_run: bool,
    diff_context: usize,
) -> PatchResult {
    let source = match std::fs::read(&request.file_abs) {
        Ok(s) => s,
        Err(e) => {
            return PatchResult::error(
                &request.file_rel,
                "file_not_found",
                format!("cannot read {}: {e}", request.file_rel),
            )
        }
    };
    let (start, end) = request.byte_range;
    if end > source.len() || start > source.len() {
        return PatchResult::error(
            &request.file_rel,
            "conflict",
            format!(
                "file shrank: byte range {start}-{end} exceeds current size ({} bytes)",
                source.len()
            ),
        );
    }
    let content = normalize_trailing_newline(&request.content, &source, start, end);
    let mut new_source = Vec::with_capacity(source.len() + content.len());
    new_source.extend_from_slice(&source[..start]);
    new_source.extend_from_slice(&content);
    new_source.extend_from_slice(&source[end..]);
    let old_slice = &source[start..end];

    let diff = unified_diff(&request.file_rel, &source, &new_source, diff_context);
    let lines_removed = count_lines(old_slice);
    let lines_added = count_lines(&content);
    let after_range = (start, start + content.len());

    let new_start_line = new_source[..after_range.0]
        .iter()
        .filter(|&&b| b == 0x0A)
        .count() as i64
        + 1;
    let inserted = if content.is_empty() {
        0
    } else {
        lines_added.max(1)
    };
    let new_end_line = if inserted > 0 {
        new_start_line + (inserted - 1).max(0)
    } else {
        new_start_line - 1
    };
    let new_content_str = String::from_utf8_lossy(&content).into_owned();

    if dry_run {
        return PatchResult {
            status: "dry_run",
            file_rel: request.file_rel.clone(),
            before_range: (start, end),
            after_range,
            new_line_range: (new_start_line, new_end_line),
            new_content: new_content_str,
            lines_removed,
            lines_added,
            diff,
            error_code: None,
            message: None,
        };
    }

    if let Err(e) = atomic_write(&request.file_abs, &new_source) {
        return PatchResult::error(
            &request.file_rel,
            "file_not_found",
            format!("write failed: {e}"),
        );
    }

    cache.invalidate(&request.file_rel);
    if let Some(mt) = mtime_f64(&request.file_abs) {
        cache.record(&CachedRead {
            file: request.file_rel.clone(),
            byte_range: after_range,
            content_hash: short_sha256(&content),
            served_at: now_f64(),
            served_mtime: mt,
            tool_call_idx: 0,
        });
    }

    PatchResult {
        status: "applied",
        file_rel: request.file_rel.clone(),
        before_range: (start, end),
        after_range,
        new_line_range: (new_start_line, new_end_line),
        new_content: new_content_str,
        lines_removed,
        lines_added,
        diff,
        error_code: None,
        message: None,
    }
}

/// Apply multiple non-overlapping edits to one file with one atomic write.
/// Range-addressed edits require fresh read-cache coverage; exact-old edits
/// prove their own context and must match exactly once.
pub fn apply_multi_patch(
    file: &str,
    edits: &[MultiPatchEdit],
    project_root: &Path,
    cache: &ReadCache,
    dry_run: bool,
    force: bool,
    diff_context: usize,
) -> MultiPatchResult {
    let canonical_root = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.to_owned());
    let input = Path::new(file);
    let candidate = if input.is_absolute() {
        input.to_owned()
    } else {
        canonical_root.join(input)
    };
    let file_abs = match candidate.canonicalize() {
        Ok(path) if path.is_file() => path,
        _ => {
            return MultiPatchResult::error(file, "file_not_found", format!("no such file: {file}"))
        }
    };
    if !file_abs.starts_with(&canonical_root) {
        return MultiPatchResult::error(
            file,
            "outside_project",
            format!("file is outside project root: {file}"),
        );
    }
    let file_rel = file_abs
        .strip_prefix(&canonical_root)
        .map(|path| path.to_string_lossy().into_owned())
        .unwrap_or_else(|_| file.to_owned());
    if edits.is_empty() {
        return MultiPatchResult::error(
            &file_rel,
            "invalid_argument",
            "edits list is empty".into(),
        );
    }
    let source = match std::fs::read(&file_abs) {
        Ok(source) => source,
        Err(error) => {
            return MultiPatchResult::error(
                &file_rel,
                "file_not_found",
                format!("cannot read {file_rel}: {error}"),
            )
        }
    };
    if looks_binary(&source) {
        return MultiPatchResult::error(
            &file_rel,
            "binary_file",
            format!("{file_rel} appears to be binary"),
        );
    }

    let mut resolved = Vec::with_capacity(edits.len());
    let mut unconfirmed = Vec::new();
    for (index, edit) in edits.iter().enumerate() {
        let Some(content) = edit.content.as_deref() else {
            return MultiPatchResult::error(
                &file_rel,
                "invalid_argument",
                format!("edit {index}: missing content"),
            );
        };
        let has_range = edit.range.is_some();
        let has_old = edit.old.is_some();
        if has_range == has_old {
            return MultiPatchResult::error(
                &file_rel,
                "invalid_argument",
                format!("edit {index}: provide exactly one of range or old"),
            );
        }
        if let Some(raw_range) = &edit.range {
            let line_range = match parse_line_range(raw_range) {
                Ok(range) => range,
                Err(_) => {
                    // Wording distinguishes malformed ("bad") vs. inverted ("invalid") ranges.
                    let shape_ok = raw_range
                        .split_once('-')
                        .is_some_and(|(a, b)| a.parse::<u64>().is_ok() && b.parse::<u64>().is_ok());
                    let what = if shape_ok { "invalid" } else { "bad" };
                    return MultiPatchResult::error(
                        &file_rel,
                        "invalid_argument",
                        format!(
                            "edit {index}: {what} range {}",
                            crate::pystr::repr(raw_range)
                        ),
                    );
                }
            };
            if line_range.1 > count_lines(&source) {
                return MultiPatchResult::error(
                    &file_rel,
                    "range_out_of_bounds",
                    format!("edit {index}: range {raw_range} exceeds file"),
                );
            }
            let start = line_start_byte(&source, line_range.0);
            let end = line_end_byte(&source, line_range.1);
            let fresh = cache
                .find_covering(&file_rel, (start, end))
                .zip(mtime_f64(&file_abs))
                .is_some_and(|(entry, mtime)| mtime_matches(entry.served_mtime, mtime));
            if !force && !fresh {
                unconfirmed.push(serde_json::json!({
                    "edit_idx": index,
                    "range": raw_range,
                }));
                continue;
            }
            resolved.push(ResolvedMultiEdit {
                index,
                start,
                end,
                content: normalize_trailing_newline(content.as_bytes(), &source, start, end),
                addressed_by: "range",
                line_range,
            });
        } else if let Some(old) = &edit.old {
            if old.is_empty() {
                return MultiPatchResult::error(
                    &file_rel,
                    "invalid_argument",
                    format!("edit {index}: old must be non-empty"),
                );
            }
            let needle = old.as_bytes();
            let hits: Vec<usize> = source
                .windows(needle.len())
                .enumerate()
                .filter_map(|(offset, window)| (window == needle).then_some(offset))
                .collect();
            if hits.is_empty() {
                return MultiPatchResult::error(
                    &file_rel,
                    "not_found",
                    format!("edit {index}: old content not found"),
                );
            }
            if hits.len() > 1 {
                let lines: Vec<usize> = hits
                    .iter()
                    .map(|offset| source[..*offset].iter().filter(|&&b| b == b'\n').count() + 1)
                    .collect();
                return MultiPatchResult::error(
                    &file_rel,
                    "ambiguous",
                    format!(
                        "edit {index}: old matches {} times at lines {lines:?}",
                        hits.len()
                    ),
                );
            }
            let start = hits[0];
            let end = start + needle.len();
            let start_line = source[..start].iter().filter(|&&b| b == b'\n').count() as i64 + 1;
            let end_line = source[..end].iter().filter(|&&b| b == b'\n').count() as i64 + 1;
            resolved.push(ResolvedMultiEdit {
                index,
                start,
                end,
                content: normalize_trailing_newline(content.as_bytes(), &source, start, end),
                addressed_by: "old",
                line_range: (start_line, end_line),
            });
        }
    }
    if !unconfirmed.is_empty() {
        return MultiPatchResult {
            status: "needs_read_confirmation",
            file_rel,
            diff: String::new(),
            per_edit: vec![],
            message: Some(format!(
                "{} edit(s) target ranges not in the read cache",
                unconfirmed.len()
            )),
            unconfirmed,
            error_code: None,
        };
    }

    resolved.sort_by_key(|edit| edit.start);
    for pair in resolved.windows(2) {
        if pair[1].start < pair[0].end {
            return MultiPatchResult::error(
                &file_rel,
                "overlapping_edits",
                format!(
                    "edits overlap: {:?} and {:?}",
                    pair[0].line_range, pair[1].line_range
                ),
            );
        }
    }
    let mut new_source = source.clone();
    for edit in resolved.iter().rev() {
        new_source.splice(edit.start..edit.end, edit.content.iter().copied());
    }

    let mut cumulative = 0isize;
    let mut per_edit = Vec::with_capacity(resolved.len());
    for edit in &resolved {
        let after_start = edit.start.saturating_add_signed(cumulative);
        let after_end = after_start + edit.content.len();
        cumulative += edit.content.len() as isize - (edit.end - edit.start) as isize;
        let new_start_line = new_source[..after_start]
            .iter()
            .filter(|&&b| b == b'\n')
            .count()
            + 1;
        let line_probe = after_end.saturating_sub(1).max(after_start);
        let new_end_line = new_source[..line_probe.min(new_source.len())]
            .iter()
            .filter(|&&b| b == b'\n')
            .count()
            + 1;
        per_edit.push(serde_json::json!({
            "edit_idx": edit.index,
            "addressed_by": edit.addressed_by,
            "before_range": [edit.start, edit.end],
            "after_range": [after_start, after_end],
            "before_lines": [edit.line_range.0, edit.line_range.1],
            "after_lines": [new_start_line, new_end_line],
        }));
    }
    let diff = unified_diff(&file_rel, &source, &new_source, diff_context);
    if dry_run {
        return MultiPatchResult {
            status: "dry_run",
            file_rel,
            diff,
            per_edit,
            unconfirmed: vec![],
            error_code: None,
            message: None,
        };
    }
    if let Err(error) = atomic_write(&file_abs, &new_source) {
        return MultiPatchResult::error(
            &file_rel,
            "file_not_found",
            format!("write failed: {error}"),
        );
    }
    cache.invalidate(&file_rel);
    if let Some(mtime) = mtime_f64(&file_abs) {
        for entry in &per_edit {
            let Some(range) = entry["after_range"].as_array() else {
                continue;
            };
            let (Some(start), Some(end)) = (range[0].as_u64(), range[1].as_u64()) else {
                continue;
            };
            let byte_range = (start as usize, end as usize);
            cache.record(&CachedRead {
                file: file_rel.clone(),
                byte_range,
                content_hash: short_sha256(&new_source[byte_range.0..byte_range.1]),
                served_at: now_f64(),
                served_mtime: mtime,
                tool_call_idx: 0,
            });
        }
    }
    MultiPatchResult {
        status: "applied",
        file_rel,
        diff,
        per_edit,
        unconfirmed: vec![],
        error_code: None,
        message: None,
    }
}

/// _normalize_trailing_newline: whole-line replacements get their terminator
/// back if the agent omitted it.
pub fn normalize_trailing_newline(
    content: &[u8],
    source: &[u8],
    _start: usize,
    end: usize,
) -> Vec<u8> {
    if content.is_empty() || content.ends_with(b"\n") {
        return content.to_vec();
    }
    if end > 0 && end <= source.len() && source[end - 1] == 0x0A {
        let mut out = content.to_vec();
        out.push(0x0A);
        return out;
    }
    content.to_vec()
}

// ---------------------------------------------------------- atomic write

pub fn atomic_write(path: &Path, data: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let original_mode = std::fs::metadata(path).ok().map(|m| {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            m.mode() & 0o7777
        }
        #[cfg(not(unix))]
        {
            let _ = m;
            0
        }
    });
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_file_name(format!(
        ".symbol.tmp-{}-{}",
        std::process::id(),
        path.file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    ));
    let result = (|| -> std::io::Result<()> {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(data)?;
        f.flush()?;
        f.sync_all()?;
        #[cfg(unix)]
        if let Some(mode) = original_mode {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(mode))?;
        }
        #[cfg(not(unix))]
        let _ = original_mode;
        std::fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

// ---------------------------------------------------------- diff

/// Full-file unified diff with each line prefixed by its absolute line number.
pub fn unified_diff(file_rel: &str, old: &[u8], new: &[u8], context: usize) -> String {
    let old_text = String::from_utf8_lossy(old);
    let new_text = String::from_utf8_lossy(new);
    let raw = crate::writes::difflib::unified_diff(
        &crate::pystr::splitlines(&old_text, true),
        &crate::pystr::splitlines(&new_text, true),
        &format!("{file_rel} (before)"),
        &format!("{file_rel} (after)"),
        context,
    );
    number_diff_lines(&raw)
}

fn number_diff_lines(diff: &str) -> String {
    static HUNK_RE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@").unwrap()
    });
    let mut out = String::with_capacity(diff.len() * 2);
    let mut old_line = 0i64;
    let mut new_line = 0i64;
    for line in crate::pystr::splitlines(diff, true) {
        if line.starts_with("@@") {
            if let Some(m) = HUNK_RE.captures(line.trim_end()) {
                old_line = m[1].parse().unwrap_or(0);
                new_line = m[2].parse().unwrap_or(0);
            }
            out.push_str(line);
        } else if line.starts_with("---") || line.starts_with("+++") {
            out.push_str(line);
        } else if let Some(rest) = line.strip_prefix(' ') {
            out.push_str(&format!(" {new_line:>4} {rest}"));
            old_line += 1;
            new_line += 1;
        } else if let Some(rest) = line.strip_prefix('-') {
            out.push_str(&format!("-{old_line:>4} {rest}"));
            old_line += 1;
        } else if let Some(rest) = line.strip_prefix('+') {
            out.push_str(&format!("+{new_line:>4} {rest}"));
            new_line += 1;
        } else {
            out.push_str(line);
        }
    }
    out
}

// ---------------------------------------------------------- misc

pub fn mtime_f64(path: &Path) -> Option<f64> {
    let meta = std::fs::metadata(path).ok()?;
    let d = meta
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?;
    // Match CPython's st_mtime conversion order. Combining nanoseconds before
    // the f64 cast loses a different low bit for modern epoch timestamps and
    // makes a freshly serialized cache entry compare stale on the next CLI.
    Some(d.as_secs() as f64 + d.subsec_nanos() as f64 * 1e-9)
}

fn mtime_matches(cached: f64, current: f64) -> bool {
    // An epoch timestamp has roughly sub-microsecond f64 precision, and JSON
    // plus platform stat conversions can round the low bits differently.
    (cached - current).abs() <= 1e-6
}

pub fn now_f64() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn short_sha256(data: &[u8]) -> String {
    // FNV-1a over the content, hex-encoded, 16 chars. Only needs to be stable
    // within one session's read cache — not cryptographic.
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_byte_math_matches_python() {
        let src = b"one\ntwo\nthree\n";
        assert_eq!(line_start_byte(src, 1), 0);
        assert_eq!(line_start_byte(src, 2), 4);
        assert_eq!(line_end_byte(src, 1), 4);
        assert_eq!(line_end_byte(src, 3), 14);
        assert_eq!(count_lines(src), 3);
        assert_eq!(count_lines(b"no newline"), 1);
        assert_eq!(count_lines(b""), 0);
    }

    #[test]
    fn trailing_newline_restored() {
        let src = b"a\nb\nc\n";
        assert_eq!(normalize_trailing_newline(b"X", src, 2, 4), b"X\n");
        assert_eq!(normalize_trailing_newline(b"X\n", src, 2, 4), b"X\n");
        assert_eq!(normalize_trailing_newline(b"", src, 2, 4), b"");
    }

    #[test]
    fn mtime_comparison_tolerates_float_rounding_only() {
        assert!(mtime_matches(
            1_783_679_868.070_986_3,
            1_783_679_868.070_986_2
        ));
        assert!(!mtime_matches(1_783_679_868.0, 1_783_679_868.001));
    }

    #[test]
    fn multi_patch_applies_non_overlapping_old_edits_atomically() {
        let temp = tempfile::tempdir().unwrap();
        let file = temp.path().join("sample.py");
        std::fs::write(&file, "alpha = 1\nbeta = 2\ngamma = 3\n").unwrap();
        let cache = ReadCache::for_session(temp.path());
        let result = apply_multi_patch(
            "sample.py",
            &[
                MultiPatchEdit {
                    range: None,
                    old: Some("alpha = 1".into()),
                    content: Some("alpha = 10".into()),
                },
                MultiPatchEdit {
                    range: None,
                    old: Some("gamma = 3".into()),
                    content: Some("gamma = 30".into()),
                },
            ],
            temp.path(),
            &cache,
            false,
            false,
            3,
        );
        assert_eq!(result.status, "applied");
        assert_eq!(result.per_edit.len(), 2);
        assert_eq!(
            std::fs::read_to_string(file).unwrap(),
            "alpha = 10\nbeta = 2\ngamma = 30\n"
        );
    }

    #[test]
    fn multi_patch_rejects_overlaps_without_writing() {
        let temp = tempfile::tempdir().unwrap();
        let file = temp.path().join("sample.py");
        let original = "alpha = 1\nbeta = 2\n";
        std::fs::write(&file, original).unwrap();
        let cache = ReadCache::for_session(temp.path());
        let result = apply_multi_patch(
            "sample.py",
            &[
                MultiPatchEdit {
                    range: None,
                    old: Some("alpha = 1\nbeta".into()),
                    content: Some("first".into()),
                },
                MultiPatchEdit {
                    range: None,
                    old: Some("beta = 2".into()),
                    content: Some("second".into()),
                },
            ],
            temp.path(),
            &cache,
            false,
            false,
            3,
        );
        assert_eq!(result.error_code.as_deref(), Some("overlapping_edits"));
        assert_eq!(std::fs::read_to_string(file).unwrap(), original);
    }
}
