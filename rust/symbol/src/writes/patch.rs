//! Port of writes/patch.py — the byte-range edit primitive: validation,
//! read-cache preflight, splice, atomic write, numbered unified diff.

use std::path::{Path, PathBuf};

use serde::Serialize;

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

// ---------------------------------------------------------- ranges

pub fn parse_line_range(raw: &str) -> Result<(i64, i64), String> {
    static RANGE_RE: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"^(\d+)-(\d+)$").unwrap());
    let m = RANGE_RE
        .captures(raw.trim())
        .ok_or_else(|| format!("range must be 'A-B' with non-negative integers, got {raw:?}"))?;
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

pub fn validate_args(
    file: &str,
    raw_range: &str,
    content: &[u8],
    project_root: &Path,
    force: bool,
) -> Result<PatchRequest, PatchResult> {
    let file_abs = match Path::new(file).canonicalize() {
        Ok(p) => p,
        Err(_) => {
            return Err(PatchResult::error(file, "file_not_found", format!("no such file: {file}")))
        }
    };
    if !file_abs.is_file() {
        return Err(PatchResult::error(file, "file_not_found", format!("not a regular file: {file}")));
    }
    let source = match std::fs::read(&file_abs) {
        Ok(s) => s,
        Err(e) => {
            return Err(PatchResult::error(file, "file_not_found", format!("cannot read {file}: {e}")))
        }
    };
    if looks_binary(&source) {
        return Err(PatchResult::error(file, "binary_file", format!("{file} appears to be binary")));
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
            format!("range end {} exceeds file length ({total} lines)", line_range.1),
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
            if covering.served_mtime == mt {
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

    let new_start_line =
        new_source[..after_range.0].iter().filter(|&&b| b == 0x0A).count() as i64 + 1;
    let inserted = if content.is_empty() { 0 } else { lines_added.max(1) };
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
        return PatchResult::error(&request.file_rel, "file_not_found", format!("write failed: {e}"));
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

/// _normalize_trailing_newline: whole-line replacements get their terminator
/// back if the agent omitted it.
pub fn normalize_trailing_newline(content: &[u8], source: &[u8], _start: usize, end: usize) -> Vec<u8> {
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
        path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
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

/// Full-file unified diff with each line prefixed by its absolute line
/// number — ports patch.py `_unified_diff` + `_number_diff_lines`.
pub fn unified_diff(file_rel: &str, old: &[u8], new: &[u8], context: usize) -> String {
    let old_text = String::from_utf8_lossy(old).into_owned();
    let new_text = String::from_utf8_lossy(new).into_owned();
    let diff = similar::TextDiff::from_lines(&old_text, &new_text);
    let raw = diff
        .unified_diff()
        .context_radius(context)
        .header(&format!("{file_rel} (before)"), &format!("{file_rel} (after)"))
        .to_string();
    number_diff_lines(&raw)
}

fn number_diff_lines(diff: &str) -> String {
    static HUNK_RE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@").unwrap()
    });
    let mut out = String::with_capacity(diff.len() * 2);
    let mut old_line = 0i64;
    let mut new_line = 0i64;
    for line in diff.split_inclusive('\n') {
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
    let d = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?;
    Some(d.as_nanos() as f64 / 1e9)
}

pub fn now_f64() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn short_sha256(data: &[u8]) -> String {
    // patch.py uses sha256[:16]; we avoid a heavy dep — this hash only has
    // to be stable within one session's read cache, not cryptographic.
    // Simple FNV-1a over the content, hex, 16 chars.
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
}
