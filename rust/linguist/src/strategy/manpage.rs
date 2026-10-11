//! Port of strategy/manpage.py. The extension pattern uses a negative
//! lookahead, so it runs on fancy-regex (filenames only — cost irrelevant).

use std::sync::LazyLock;

use super::Strategy;
use crate::blob::Blob;
use crate::language::{LangId, Registry};

static MANPAGE_EXTS: LazyLock<fancy_regex::Regex> = LazyLock::new(|| {
    fancy_regex::Regex::new(r"(?i)\.(?:[1-9](?![0-9])[a-z_0-9]*|0p|n|man|mdoc)(?:\.in)?$").unwrap()
});

pub struct ManpageStrategy;

impl Strategy for ManpageStrategy {
    fn call(&self, blob: &Blob, candidates: &[LangId], reg: &Registry) -> Vec<LangId> {
        if !candidates.is_empty() {
            return candidates.to_vec();
        }
        if MANPAGE_EXTS.is_match(&blob.name).unwrap_or(false) {
            return ["Roff Manpage", "Roff"]
                .iter()
                .filter_map(|n| reg.find_by_name(n))
                .collect();
        }
        vec![]
    }
}
