//! Port of caches/disk.py — the JSON-file read cache, format-compatible so
//! Python and Rust CLIs can share one SYMBOL_SESSION_ID session.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone)]
pub struct CachedRead {
    pub file: String,
    pub byte_range: (usize, usize),
    pub content_hash: String,
    pub served_at: f64,
    pub served_mtime: f64,
    pub tool_call_idx: i64,
}

pub struct ReadCache {
    path: Option<PathBuf>, // None = null cache (no SYMBOL_SESSION_ID)
}

impl ReadCache {
    /// DiskReadCache when SYMBOL_SESSION_ID is set, else a null cache —
    /// matching how the Python CLI picks its backing.
    pub fn for_session(project_root: &Path) -> ReadCache {
        match std::env::var("SYMBOL_SESSION_ID") {
            Ok(sid) if !sid.is_empty() => ReadCache {
                path: Some(project_root.join(".symbol/cache/sessions").join(format!("{sid}.json"))),
            },
            _ => ReadCache { path: None },
        }
    }

    fn read(&self) -> serde_json::Map<String, serde_json::Value> {
        let Some(path) = &self.path else {
            return Default::default();
        };
        std::fs::read_to_string(path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    fn write(&self, entries: &serde_json::Map<String, serde_json::Value>) {
        let Some(path) = &self.path else { return };
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let tmp = path.with_extension(format!("tmp.{}", std::process::id()));
        if std::fs::write(&tmp, serde_json::to_string(entries).unwrap_or_default()).is_ok() {
            let _ = std::fs::rename(&tmp, path);
        }
    }

    pub fn record(&self, entry: &CachedRead) {
        if self.path.is_none() {
            return;
        }
        let mut entries = self.read();
        let key = format!("{}:{}-{}", entry.file, entry.byte_range.0, entry.byte_range.1);
        entries.insert(key, serde_json::to_value(entry).unwrap());
        self.write(&entries);
    }

    pub fn find_covering(&self, file: &str, byte_range: (usize, usize)) -> Option<CachedRead> {
        let entries = self.read();
        let mut best: Option<CachedRead> = None;
        for raw in entries.values() {
            if raw.get("file").and_then(|f| f.as_str()) != Some(file) {
                continue;
            }
            let Ok(entry) = serde_json::from_value::<CachedRead>(raw.clone()) else {
                continue;
            };
            if entry.byte_range.0 <= byte_range.0 && entry.byte_range.1 >= byte_range.1 {
                if best.as_ref().map_or(true, |b| entry.served_at > b.served_at) {
                    best = Some(entry);
                }
            }
        }
        best
    }

    pub fn invalidate(&self, file: &str) {
        if self.path.is_none() {
            return;
        }
        let mut entries = self.read();
        entries.retain(|_, v| v.get("file").and_then(|f| f.as_str()) != Some(file));
        self.write(&entries);
    }
}
