//! Python fnmatch semantics (`*` crosses `/`, `?`, `[seq]`, `[!seq]`,
//! case-sensitive on posix) — hand-ported from fnmatch.translate because
//! globset's defaults differ subtly. Plus the `**/`-prefix retry from
//! linguist.py `_exc_match`.

pub struct GlobPat {
    re: regex::Regex,
    /// compiled from pat[3..] when the pattern starts with "**/"
    stripped_re: Option<regex::Regex>,
}

/// fnmatch.translate: glob → regex body. Anchored `\A(?s:...)\z` at compile.
fn translate(pat: &str) -> String {
    let mut out = String::with_capacity(pat.len() * 2);
    let chars: Vec<char> = pat.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        i += 1;
        match c {
            '*' => out.push_str(".*"),
            '?' => out.push('.'),
            '[' => {
                // find closing ]
                let mut j = i;
                if j < chars.len() && (chars[j] == '!' || chars[j] == ']') {
                    j += 1;
                    if j < chars.len() && chars[j] == ']' && chars[j - 1] == '!' {
                        j += 1;
                    }
                }
                while j < chars.len() && chars[j] != ']' {
                    j += 1;
                }
                if j >= chars.len() {
                    out.push_str(r"\[");
                } else {
                    let inner: String = chars[i..j].iter().collect();
                    let inner = inner.replace('\\', r"\\");
                    out.push('[');
                    if let Some(rest) = inner.strip_prefix('!') {
                        out.push('^');
                        out.push_str(rest);
                    } else if let Some(rest) = inner.strip_prefix('^') {
                        out.push_str(r"\^");
                        out.push_str(rest);
                    } else {
                        out.push_str(&inner);
                    }
                    out.push(']');
                    i = j + 1;
                }
            }
            c => out.push_str(&regex::escape(&c.to_string())),
        }
    }
    out
}

fn compile(pat: &str) -> Option<regex::Regex> {
    regex::Regex::new(&format!(r"\A(?s:{})\z", translate(pat))).ok()
}

impl GlobPat {
    pub fn new(pat: &str) -> Option<GlobPat> {
        Some(GlobPat {
            re: compile(pat)?,
            stripped_re: pat.strip_prefix("**/").and_then(compile),
        })
    }

    /// linguist.py `_exc_match`: fnmatch(rel, pat), retrying with the
    /// `**/` prefix stripped.
    pub fn matches(&self, rel: &str) -> bool {
        self.re.is_match(rel) || self.stripped_re.as_ref().is_some_and(|r| r.is_match(rel))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_fnmatch_semantics() {
        // * crosses path separators in fnmatch (unlike shell globs)
        assert!(GlobPat::new("*.py").unwrap().matches("a/b/c.py"));
        assert!(GlobPat::new("dist").unwrap().matches("dist"));
        assert!(!GlobPat::new("dist").unwrap().matches("a/dist"));
        assert!(GlobPat::new("**/dist").unwrap().matches("a/dist"));
        // **/ retry also matches bare name (pat[3:] == "dist")
        assert!(GlobPat::new("**/dist").unwrap().matches("dist"));
        assert!(GlobPat::new("test?.py").unwrap().matches("test1.py"));
        assert!(GlobPat::new("[ab].py").unwrap().matches("a.py"));
        assert!(!GlobPat::new("[!ab].py").unwrap().matches("a.py"));
        // case-sensitive
        assert!(!GlobPat::new("*.PY").unwrap().matches("a.py"));
    }
}
