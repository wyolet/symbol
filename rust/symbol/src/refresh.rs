//! Port of commands/refresh.py — reindex (incremental or full) and clear
//! the transaction log. Shared by `symbol refresh` and the MCP Refresh tool.

use std::path::Path;

use anyhow::Result;

use crate::adapters::Registry;
use crate::index::{full_build, get_or_build, Index};
use crate::writes::tx::clear_transactions;

pub struct RefreshResult {
    /// disk | refresh | rebuild | build | compact | full
    pub source: &'static str,
    pub transactions_cleared: usize,
}

/// `full` rebuilds from scratch; otherwise only changed files are re-parsed.
/// The index is replaced only after a successful build, so a failed full
/// rebuild keeps the previous one. Transactions are cleared afterwards
/// unless `keep_transactions`.
pub fn refresh(
    project_root: &Path,
    reg: &Registry,
    full: bool,
    keep_transactions: bool,
) -> Result<(Index, RefreshResult)> {
    let (index, source) = if full {
        let index = full_build(project_root, reg)?;
        index.save()?;
        (index, "full")
    } else {
        get_or_build(project_root, reg)?
    };
    let transactions_cleared = if keep_transactions {
        0
    } else {
        clear_transactions(project_root)
    };
    Ok((
        index,
        RefreshResult {
            source,
            transactions_cleared,
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clears_transactions_unless_kept() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        let tx = root.join(".symbol/transactions");
        std::fs::create_dir_all(tx.join("20260101-a")).unwrap();
        std::fs::create_dir_all(tx.join("20260101-b.undone")).unwrap();
        let reg = Registry::new(&root);

        let (_, kept) = refresh(&root, &reg, false, true).unwrap();
        assert_eq!(kept.transactions_cleared, 0);
        assert!(tx.join("20260101-a").is_dir());

        let (index, cleared) = refresh(&root, &reg, true, false).unwrap();
        assert_eq!(cleared.source, "full");
        assert_eq!(cleared.transactions_cleared, 2);
        assert!(index.files.is_empty());
        assert!(!tx.join("20260101-a").exists());
    }
}
