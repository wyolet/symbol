//! Port of strategy/extension.py — with the generic.yml gate: filenames
//! ending in a "generic" extension defer to other strategies untouched.

use super::{narrow, Strategy};
use crate::blob::Blob;
use crate::language::{LangId, Registry};

pub struct ExtensionStrategy {
    generic_extensions: Vec<String>,
}

impl ExtensionStrategy {
    pub fn new(generic_extensions: Vec<String>) -> Self {
        ExtensionStrategy { generic_extensions }
    }

    fn is_generic(&self, filename: &str) -> bool {
        let f = filename.to_lowercase();
        self.generic_extensions.iter().any(|ext| f.ends_with(ext))
    }
}

impl Strategy for ExtensionStrategy {
    fn call(&self, blob: &Blob, candidates: &[LangId], reg: &Registry) -> Vec<LangId> {
        if self.is_generic(&blob.name) {
            return candidates.to_vec();
        }
        narrow(reg.find_by_extension(&blob.name), candidates)
    }
}
