//! Multi-file atomic commit with pre-image capture, persisted undo log in
//! `.symbol/transactions/<id>/`. `symbol undo` reads the manifest JSON directly.

use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::writes::patch::atomic_write;

const TX_DIR: &str = ".symbol/transactions";
const MANIFEST: &str = "manifest.json";
const TX_KEEP: usize = 50;

pub struct FileEdit {
    pub file_abs: PathBuf,
    pub file_rel: String,
    pub new_content: Vec<u8>,
}

#[derive(Serialize)]
pub struct TransactionResult {
    pub status: &'static str, // committed | error
    pub files_written: Vec<String>,
    pub transaction_id: Option<String>,
    pub error_code: Option<String>,
    pub message: Option<String>,
}

pub fn commit_edits(
    edits: &[FileEdit],
    project_root: &Path,
    op_name: &str,
    subject: &str,
    dry_run: bool,
) -> TransactionResult {
    if dry_run {
        return TransactionResult {
            status: "committed",
            files_written: edits.iter().map(|e| e.file_rel.clone()).collect(),
            transaction_id: None,
            error_code: None,
            message: None,
        };
    }

    // Pre-image capture. None = file doesn't exist yet.
    let mut pre_images: Vec<(String, Option<Vec<u8>>)> = vec![];
    for edit in edits {
        match std::fs::read(&edit.file_abs) {
            Ok(bytes) => pre_images.push((edit.file_rel.clone(), Some(bytes))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                pre_images.push((edit.file_rel.clone(), None))
            }
            Err(e) => {
                return TransactionResult {
                    status: "error",
                    files_written: vec![],
                    transaction_id: None,
                    error_code: Some("read_failed".into()),
                    message: Some(format!(
                        "cannot read {} for pre-image capture: {e}",
                        edit.file_rel
                    )),
                }
            }
        }
    }

    for (written, edit) in edits.iter().enumerate() {
        if let Err(e) = atomic_write(&edit.file_abs, &edit.new_content) {
            // Rollback everything already written.
            for (i, done) in edits.iter().enumerate().take(written) {
                match &pre_images[i].1 {
                    None => {
                        let _ = std::fs::remove_file(&done.file_abs);
                    }
                    Some(pre) => {
                        let _ = atomic_write(&done.file_abs, pre);
                    }
                }
            }
            return TransactionResult {
                status: "error",
                files_written: vec![],
                transaction_id: None,
                error_code: Some("write_failed".into()),
                message: Some(format!(
                    "partial write rolled back ({written}/{} files reverted): {e}",
                    edits.len()
                )),
            };
        }
    }

    let tx_id = persist_transaction(project_root, op_name, subject, edits, &pre_images);

    TransactionResult {
        status: "committed",
        files_written: edits.iter().map(|e| e.file_rel.clone()).collect(),
        transaction_id: tx_id,
        error_code: None,
        message: None,
    }
}

fn persist_transaction(
    project_root: &Path,
    op_name: &str,
    subject: &str,
    edits: &[FileEdit],
    pre_images: &[(String, Option<Vec<u8>>)],
) -> Option<String> {
    let tx_root = project_root.join(TX_DIR);
    std::fs::create_dir_all(&tx_root).ok()?;
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_millis();
    let tx_id = format!("{millis}-{op_name}");
    let tx_dir = tx_root.join(&tx_id);
    std::fs::create_dir(&tx_dir).ok()?;

    let mut manifest_files: Vec<serde_json::Value> = vec![];
    for (i, edit) in edits.iter().enumerate() {
        let mut entry = serde_json::json!({ "file_rel": edit.file_rel, "pre_image": null });
        if let Some(pre) = &pre_images[i].1 {
            let digest = hex_hash(edit.file_rel.as_bytes());
            let blob_name = format!("{digest}.pre");
            if std::fs::write(tx_dir.join(&blob_name), pre).is_ok() {
                entry["pre_image"] = blob_name.into();
            } else {
                entry["pre_image_error"] = true.into();
            }
        }
        manifest_files.push(entry);
    }

    let manifest = serde_json::json!({
        "version": 1,
        "id": tx_id,
        "op": op_name,
        "subject": subject,
        "created_at": crate::writes::patch::now_f64(),
        "files": manifest_files,
    });
    std::fs::write(
        tx_dir.join(MANIFEST),
        serde_json::to_string_pretty(&manifest).ok()?,
    )
    .ok()?;

    prune_transactions(&tx_root, TX_KEEP);
    Some(tx_id)
}

fn prune_transactions(tx_root: &Path, keep: usize) {
    let Ok(entries) = std::fs::read_dir(tx_root) else {
        return;
    };
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .filter(|e| e.path().is_dir())
        .map(|e| e.path())
        .collect();
    dirs.sort();
    if dirs.len() <= keep {
        return;
    }
    let excess = dirs.len() - keep;
    for old in &dirs[..excess] {
        let _ = std::fs::remove_dir_all(old);
    }
}

/// Remove every transaction directory (undone ones included); returns how
/// many were removed.
pub fn clear_transactions(project_root: &Path) -> usize {
    let Ok(entries) = std::fs::read_dir(project_root.join(TX_DIR)) else {
        return 0;
    };
    entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir() && std::fs::remove_dir_all(p).is_ok())
        .count()
}

// ---------------------------------------------------------- undo

#[derive(Serialize)]
pub struct UndoResult {
    pub status: &'static str, // undone | nothing_to_undo | error
    pub transaction_id: Option<String>,
    pub op: Option<String>,
    pub subject: Option<String>,
    pub files_restored: Vec<String>,
    pub files_skipped: Vec<String>,
    pub error_code: Option<String>,
    pub message: Option<String>,
}

pub fn undo_last(project_root: &Path) -> UndoResult {
    let nothing = UndoResult {
        status: "nothing_to_undo",
        transaction_id: None,
        op: None,
        subject: None,
        files_restored: vec![],
        files_skipped: vec![],
        error_code: None,
        message: None,
    };
    let tx_root = project_root.join(TX_DIR);
    if !tx_root.is_dir() {
        return nothing;
    }
    let Ok(entries) = std::fs::read_dir(&tx_root) else {
        return nothing;
    };
    let mut candidates: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir() && !p.to_string_lossy().ends_with(".undone"))
        .collect();
    candidates.sort();
    let Some(tx_dir) = candidates.pop() else {
        return nothing;
    };

    let manifest: serde_json::Value = match std::fs::read_to_string(tx_dir.join(MANIFEST))
        .map_err(|e| e.to_string())
        .and_then(|s| serde_json::from_str(&s).map_err(|e| e.to_string()))
    {
        Ok(m) => m,
        Err(e) => {
            return UndoResult {
                status: "error",
                transaction_id: Some(tx_dir.file_name().unwrap().to_string_lossy().into_owned()),
                op: None,
                subject: None,
                files_restored: vec![],
                files_skipped: vec![],
                error_code: Some("manifest_unreadable".into()),
                message: Some(e),
            }
        }
    };

    let mut restored = vec![];
    let mut skipped = vec![];
    for entry in manifest["files"].as_array().cloned().unwrap_or_default() {
        let Some(file_rel) = entry["file_rel"].as_str() else {
            continue;
        };
        let target = project_root.join(file_rel);
        let outcome = match entry["pre_image"].as_str() {
            None => std::fs::remove_file(&target).or_else(|e| {
                if e.kind() == std::io::ErrorKind::NotFound {
                    Ok(())
                } else {
                    Err(e)
                }
            }),
            Some(blob) => {
                std::fs::read(tx_dir.join(blob)).and_then(|pre| atomic_write(&target, &pre))
            }
        };
        match outcome {
            Ok(()) => restored.push(file_rel.to_owned()),
            Err(_) => skipped.push(file_rel.to_owned()),
        }
    }

    let _ = std::fs::rename(
        &tx_dir,
        tx_dir.with_file_name(format!(
            "{}.undone",
            tx_dir.file_name().unwrap().to_string_lossy()
        )),
    );

    UndoResult {
        status: "undone",
        transaction_id: manifest["id"].as_str().map(str::to_owned),
        op: manifest["op"].as_str().map(str::to_owned),
        subject: manifest["subject"].as_str().map(str::to_owned),
        files_restored: restored,
        files_skipped: skipped,
        error_code: None,
        message: None,
    }
}

fn hex_hash(data: &[u8]) -> String {
    // Python uses sha256(file_rel)[:16] purely as a filename; any stable
    // 16-hex-char digest works since the manifest maps rel→blob explicitly.
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}
