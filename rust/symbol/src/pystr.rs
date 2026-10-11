//! Python `str` behaviors that surface in user-visible output: `repr()` in
//! error messages and `str.splitlines()` in body slicing. Matching them
//! keeps messages and payloads identical to the Python implementation.

/// `repr(s)` for a Python `str`.
pub fn repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c if (c as u32) < 0x7f || is_printable(c) => out.push(c),
            c if (c as u32) <= 0xff => out.push_str(&format!("\\x{:02x}", c as u32)),
            c if (c as u32) <= 0xffff => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push_str(&format!("\\U{:08x}", c as u32)),
        }
    }
    out.push(quote);
    out
}

/// Approximates `str.isprintable()` for non-ASCII: separators, format and
/// private-use characters are escaped; unassigned code points are not known
/// here and print as-is.
fn is_printable(c: char) -> bool {
    let cp = c as u32;
    !matches!(cp,
        0x80..=0xa0
        | 0xad
        | 0x600..=0x605
        | 0x61c
        | 0x6dd
        | 0x70f
        | 0x1680
        | 0x180e
        | 0x2000..=0x200f
        | 0x2028..=0x202f
        | 0x205f..=0x2064
        | 0x2066..=0x206f
        | 0x3000
        | 0xe000..=0xf8ff
        | 0xfeff
        | 0xfff9..=0xfffb
        | 0xf0000..)
}

/// `str.splitlines(keepends)`: Python also breaks on \v, \f, \x1c-\x1e,
/// \x85, U+2028 and U+2029, not only \n / \r\n / \r.
pub fn splitlines(s: &str, keepends: bool) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let mut end = i + c.len_utf8();
        let is_break = match c {
            '\r' => {
                if let Some(&(_, '\n')) = chars.peek() {
                    chars.next();
                    end += 1;
                }
                true
            }
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}'
            | '\u{2029}' => true,
            _ => false,
        };
        if is_break {
            out.push(if keepends {
                &s[start..end]
            } else {
                &s[start..i]
            });
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_matches_python() {
        assert_eq!(repr("abc"), "'abc'");
        assert_eq!(repr("it's"), "\"it's\"");
        assert_eq!(repr("a'b\"c"), "'a\\'b\"c'");
        assert_eq!(repr("tab\there\n"), "'tab\\there\\n'");
        assert_eq!(repr("back\\slash"), "'back\\\\slash'");
        assert_eq!(repr("a\nb\\"), "'a\\nb\\\\'");
        assert_eq!(repr("\x01"), "'\\x01'");
        assert_eq!(repr("→ é"), "'→ é'");
        assert_eq!(repr("\u{a0}"), "'\\xa0'");
        assert_eq!(repr("\u{2028}"), "'\\u2028'");
    }

    #[test]
    fn splitlines_matches_python() {
        assert_eq!(
            splitlines("a\nb\r\nc\rd", true),
            ["a\n", "b\r\n", "c\r", "d"]
        );
        assert_eq!(splitlines("a\nb\n", false), ["a", "b"]);
        assert_eq!(splitlines("a\x0cb\u{2028}c", false), ["a", "b", "c"]);
        assert_eq!(
            splitlines("a\r\nb\rc\x0cd\u{2028}e", true),
            ["a\r\n", "b\r", "c\x0c", "d\u{2028}", "e"]
        );
        assert!(splitlines("", true).is_empty());
        assert_eq!(splitlines("\n\n", true), ["\n", "\n"]);
    }
}
