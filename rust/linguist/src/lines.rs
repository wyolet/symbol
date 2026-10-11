//! Byte-exact ports of the CPython text primitives the Python linguist
//! relies on: `bytes.decode(errors="ignore")`, `str.splitlines()`, and the
//! blank-line test behind `Blob.sloc` (`str.strip()` / `str.isspace()`).

/// `bytes.decode("utf-8", errors="ignore")` — invalid sequences are DROPPED,
/// not replaced with U+FFFD (which would flip blank lines to non-blank and
/// change sloc counts). Both CPython and Rust delimit invalid sequences by
/// the Unicode "maximal subpart" rule, so skipping `error_len()` bytes agrees
/// with CPython's ignore handler; an incomplete trailing sequence is dropped.
pub fn decode_ignore(bytes: &[u8]) -> String {
    if let Ok(s) = std::str::from_utf8(bytes) {
        return s.to_owned();
    }
    let mut out = String::with_capacity(bytes.len());
    let mut rest = bytes;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.push_str(s);
                break;
            }
            Err(e) => {
                let valid = e.valid_up_to();
                // SAFETY: from_utf8 just validated rest[..valid].
                out.push_str(unsafe { std::str::from_utf8_unchecked(&rest[..valid]) });
                match e.error_len() {
                    Some(n) => rest = &rest[valid + n..],
                    None => break, // incomplete sequence at end of input
                }
            }
        }
    }
    out
}

/// The line boundaries `str.splitlines()` recognizes. `\r\n` is handled as a
/// single boundary in `py_splitlines`.
const IS_BOUNDARY: fn(char) -> bool = |c| {
    matches!(
        c,
        '\n' | '\r'
            | '\u{0b}'
            | '\u{0c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
};

/// `str.splitlines()`: byte ranges of line contents (terminators excluded).
/// A trailing terminator does NOT produce an empty final line.
pub fn py_splitlines(s: &str) -> Vec<(usize, usize)> {
    let mut lines = Vec::new();
    let mut start = 0usize;
    let mut iter = s.char_indices().peekable();
    while let Some((i, c)) = iter.next() {
        if IS_BOUNDARY(c) {
            lines.push((start, i));
            let mut next = i + c.len_utf8();
            if c == '\r' {
                if let Some(&(j, '\n')) = iter.peek() {
                    iter.next();
                    next = j + 1;
                }
            }
            start = next;
        }
    }
    if start < s.len() {
        lines.push((start, s.len()));
    }
    lines
}

/// Python `str.isspace()` per char: Unicode White_Space plus \x1c..\x1f
/// (which Rust's `char::is_whitespace` excludes).
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// A line is blank when `line.strip()` is empty — i.e. every char isspace.
pub fn is_blank(line: &str) -> bool {
    line.chars().all(py_isspace)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn split<'a>(s: &'a str) -> Vec<&'a str> {
        py_splitlines(s).into_iter().map(|(a, b)| &s[a..b]).collect()
    }

    #[test]
    fn splitlines_matches_cpython() {
        // expected values generated with CPython 3.13 str.splitlines()
        assert_eq!(split("a\r\nb"), vec!["a", "b"]);
        assert_eq!(split("a\rb"), vec!["a", "b"]);
        assert_eq!(split("a\u{85}b"), vec!["a", "b"]);
        assert_eq!(split("a\u{2028}b"), vec!["a", "b"]);
        assert_eq!(split("a\u{2029}b"), vec!["a", "b"]);
        assert_eq!(split("a\u{0b}b\u{0c}c"), vec!["a", "b", "c"]);
        assert_eq!(split("a\u{1c}b\u{1d}c\u{1e}d"), vec!["a", "b", "c", "d"]);
        assert_eq!(split("a\n"), vec!["a"]);
        assert_eq!(split("\n\n"), vec!["", ""]);
        assert_eq!(split(""), Vec::<&str>::new());
        assert_eq!(split("\r\n"), vec![""]);
        assert_eq!(split("a\n\rb"), vec!["a", "", "b"]);
    }

    #[test]
    fn blank_matches_cpython_strip() {
        assert!(is_blank(""));
        assert!(is_blank(" \t"));
        assert!(is_blank("\u{a0}")); // NBSP is stripped by str.strip()
        assert!(is_blank("\u{1f}")); // \x1f isspace in Python, not in Rust
        assert!(!is_blank(" x "));
    }

    #[test]
    fn decode_ignore_drops_invalid() {
        // CPython: b"a\xffb".decode("utf-8", "ignore") == "ab"
        assert_eq!(decode_ignore(b"a\xffb"), "ab");
        // truncated multibyte at end is dropped: b"a\xe2\x82".decode(...,"ignore") == "a"
        assert_eq!(decode_ignore(b"a\xe2\x82"), "a");
        // valid multibyte passes through
        assert_eq!(decode_ignore("héllo".as_bytes()), "héllo");
        // overlong/invalid interior sequence
        assert_eq!(decode_ignore(b"\xf0\x28\x8c\x28"), "((");
    }
}
