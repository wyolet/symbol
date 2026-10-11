//! Two-engine regex wrapper. `regex` for everything it can compile (fast,
//! no backtracking); `fancy-regex` for patterns needing lookaround; drop as
//! the last resort — the same compile-or-drop policy as Python's
//! `load_vendor_regex` (config/load.py).

pub enum Re {
    Fast(regex::Regex),
    Fancy(Box<fancy_regex::Regex>),
}

impl Re {
    /// Try `regex` first, fall back to `fancy-regex`, `None` = drop.
    pub fn compile(pattern: &str) -> Option<Re> {
        if let Ok(r) = regex::Regex::new(pattern) {
            return Some(Re::Fast(r));
        }
        fancy_regex::Regex::new(pattern)
            .ok()
            .map(|r| Re::Fancy(Box::new(r)))
    }

    /// Unanchored search (Ruby `=~` / Python `re.search` semantics).
    pub fn is_match(&self, hay: &str) -> bool {
        match self {
            Re::Fast(r) => r.is_match(hay),
            Re::Fancy(r) => r.is_match(hay).unwrap_or(false),
        }
    }

    /// First match's capture group 1, if any.
    pub fn capture_1(&self, hay: &str) -> Option<String> {
        match self {
            Re::Fast(r) => r
                .captures(hay)
                .and_then(|c| c.get(1))
                .map(|m| m.as_str().to_owned()),
            Re::Fancy(r) => r
                .captures(hay)
                .ok()
                .flatten()
                .and_then(|c| c.get(1))
                .map(|m| m.as_str().to_owned()),
        }
    }
}
