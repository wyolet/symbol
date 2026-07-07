//! Port of strategy/filename.py.

use super::{narrow, Strategy};
use crate::blob::Blob;
use crate::language::{LangId, Registry};

pub struct FilenameStrategy;

impl Strategy for FilenameStrategy {
    fn call(&self, blob: &Blob, candidates: &[LangId], reg: &Registry) -> Vec<LangId> {
        // _dedup by language_id, preserving order
        let mut seen = std::collections::HashSet::new();
        let found: Vec<LangId> = reg
            .find_by_filename(&blob.name)
            .iter()
            .copied()
            .filter(|&id| seen.insert(reg.get(id).language_id))
            .collect();
        // Python: `if candidates: return [l for l in languages if l in candidates]`
        // — note this returns [] when found is empty too, same as narrow().
        narrow(&found, candidates)
    }
}
