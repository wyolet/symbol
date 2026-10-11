//! Port of strategy/modeline.py. Both patterns need lookaround →
//! fancy-regex; they run on ≤10 short lines per file, so backtracking
//! cost is irrelevant.
//!
//! Bug-faithful details preserved from Python:
//! - candidates are IGNORED (the Python signature takes them and never
//!   reads them) — a modeline hit decides immediately, no narrowing.
//! - header and footer are concatenated with NO separator between them
//!   (`header + footer`), so a pattern can in principle span the seam.

use std::sync::LazyLock;

use super::Strategy;
use crate::blob::Blob;
use crate::language::{LangId, Registry};

const SEARCH_SCOPE: usize = 5;

static EMACS_MODELINE: LazyLock<fancy_regex::Regex> = LazyLock::new(|| {
    fancy_regex::Regex::new(
        r"(?i)-\*-(?:[ \t]*(?=[^:;\s]+[ \t]*-\*-)|(?:.*?[ \t;]|(?<=-\*-))[ \t]*mode[ \t]*:[ \t]*)([^:;\s]+)(?=[ \t;]|(?<![-*])-\*-).*?-\*-",
    )
    .unwrap()
});

static VIM_MODELINE: LazyLock<fancy_regex::Regex> = LazyLock::new(|| {
    fancy_regex::Regex::new(
        r"(?i)(?:(?:^|[ \t])(?:vi|Vi(?=m))(?:m[<=>]?[0-9]+|m)?|[ \t]ex)(?=:[ \t]*set?[ \t][^\r\n:]+:|:(?![ \t]*set?[ \t]))(?:(?:[ \t]*:[ \t]*|[ \t])\w*(?:[ \t]*=(?:[^\\\s]|\\.)*)?)*[ \t:](?:filetype|ft|syntax)[ \t]*=(\w+)(?=$|\s|:)",
    )
    .unwrap()
});

pub struct ModeLineStrategy;

impl Strategy for ModeLineStrategy {
    fn call(&self, blob: &Blob, _candidates: &[LangId], reg: &Registry) -> Vec<LangId> {
        if blob.is_symlink {
            return vec![];
        }
        let header = blob.first_lines(SEARCH_SCOPE).join("\n");
        if header.contains("UseVimball") {
            return vec![];
        }
        let footer = blob.last_lines(SEARCH_SCOPE).join("\n");
        let data = header + &footer;

        for re in [&*EMACS_MODELINE, &*VIM_MODELINE] {
            if let Ok(Some(caps)) = re.captures(&data) {
                if let Some(m) = caps.get(1) {
                    return reg.find_by_alias(m.as_str()).into_iter().collect();
                }
            }
        }
        vec![]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{load, ConfigSource};
    use std::path::Path;

    fn detect(content: &str) -> Option<String> {
        let reg = Registry::load(&load(&ConfigSource::Embedded).unwrap()).unwrap();
        let blob = Blob::from_bytes(
            Path::new("x.txt"),
            content.as_bytes().to_vec(),
            false,
            content.len() as u64,
        );
        let found = ModeLineStrategy.call(&blob, &[], &reg);
        found.first().map(|&id| reg.get(id).name.clone())
    }

    #[test]
    fn emacs_modeline() {
        assert_eq!(detect("; -*- mode: ruby -*-\ncode"), Some("Ruby".into()));
        assert_eq!(detect("// -*- c++ -*-\ncode"), Some("C++".into()));
    }

    #[test]
    fn vim_modeline() {
        assert_eq!(
            detect("code\n# vim: set ft=python :\n"),
            Some("Python".into())
        );
        // needs ≥10 lines: in short files the no-separator header+footer
        // concat merges the modeline with the first line ("gotext") —
        // verified identical in Python's ModeLineStrategy
        assert_eq!(
            detect("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\n// vim: syntax=go\n"),
            Some("Go".into())
        );
        assert_eq!(detect("text\n// vim: syntax=go\n"), None);
    }

    #[test]
    fn vimball_early_out() {
        assert_eq!(detect("UseVimball\nvim: set ft=python :"), None);
    }
}
