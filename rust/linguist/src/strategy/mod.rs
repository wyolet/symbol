//! Port of shared/linguist/strategy/ — one module per Python strategy file.
//! Contract (linguist.py detect): return 1 candidate = decide, >1 = narrow,
//! 0 = keep the previous candidate set.

pub mod extension;
pub mod filename;
pub mod heuristics;
pub mod manpage;
pub mod modeline;
pub mod shebang;
pub mod xml;

use crate::blob::Blob;
use crate::language::{LangId, Registry};

pub trait Strategy {
    fn call(&self, blob: &Blob, candidates: &[LangId], reg: &Registry) -> Vec<LangId>;
}

/// `[lang for lang in languages if lang in candidates] if candidates else languages`
/// — the narrowing idiom shared by filename/extension/shebang.
pub(crate) fn narrow(found: &[LangId], candidates: &[LangId]) -> Vec<LangId> {
    if candidates.is_empty() {
        found.to_vec()
    } else {
        found.iter().copied().filter(|id| candidates.contains(id)).collect()
    }
}
