//! Port of strategy/xml.py.

use std::sync::LazyLock;

use super::Strategy;
use crate::blob::Blob;
use crate::language::{LangId, Registry};

static XML_HEADER: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"(?i)<\?xml\s+version=").unwrap());

pub struct XMLStrategy;

impl Strategy for XMLStrategy {
    fn call(&self, blob: &Blob, candidates: &[LangId], reg: &Registry) -> Vec<LangId> {
        if !candidates.is_empty() {
            return candidates.to_vec();
        }
        let header = blob.first_lines(2).join("\n");
        if XML_HEADER.is_match(&header) {
            return reg.find_by_name("XML").into_iter().collect();
        }
        vec![]
    }
}
