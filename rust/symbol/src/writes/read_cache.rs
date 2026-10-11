//! Read cache that patch preflight consults. The disk backing persists across
//! tool calls sharing a SYMBOL_SESSION_ID; the in-memory backing serves the
//! long-lived MCP server.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

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

enum Backing {
    Null,
    Disk(PathBuf),
    // Insertion-ordered like the Python dict, keyed by (file, byte_range).
    Memory(Mutex<Vec<CachedRead>>),
}

pub struct ReadCache {
    backing: Backing,
}

impl ReadCache {
    /// DiskReadCache when SYMBOL_SESSION_ID is set, else a null cache —
    /// matching how the Python CLI picks its backing.
    pub fn for_session(project_root: &Path) -> ReadCache {
        let backing = match std::env::var("SYMBOL_SESSION_ID") {
            Ok(sid) if !sid.is_empty() => Backing::Disk(
                project_root
                    .join(".symbol/cache/sessions")
                    .join(format!("{sid}.json")),
            ),
            _ => Backing::Null,
        };
        ReadCache { backing }
    }

    /// Process-scoped cache for one MCP session.
    pub fn in_memory() -> ReadCache {
        ReadCache {
            backing: Backing::Memory(Mutex::new(vec![])),
        }
    }

    fn read_disk(path: &Path) -> serde_json::Map<String, serde_json::Value> {
        std::fs::read_to_string(path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    fn write_disk(path: &Path, entries: &serde_json::Map<String, serde_json::Value>) {
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let tmp = path.with_extension(format!("tmp.{}", std::process::id()));
        if std::fs::write(&tmp, serde_json::to_string(entries).unwrap_or_default()).is_ok() {
            let _ = std::fs::rename(&tmp, path);
        }
    }

    pub fn record(&self, entry: &CachedRead) {
        match &self.backing {
            Backing::Null => {}
            Backing::Disk(path) => {
                let mut entries = Self::read_disk(path);
                let key = format!(
                    "{}:{}-{}",
                    entry.file, entry.byte_range.0, entry.byte_range.1
                );
                entries.insert(key, serde_json::to_value(entry).unwrap());
                Self::write_disk(path, &entries);
            }
            Backing::Memory(entries) => {
                let mut entries = entries.lock().unwrap();
                match entries
                    .iter_mut()
                    .find(|e| e.file == entry.file && e.byte_range == entry.byte_range)
                {
                    Some(existing) => *existing = entry.clone(),
                    None => entries.push(entry.clone()),
                }
            }
        }
    }

    pub fn find_covering(&self, file: &str, byte_range: (usize, usize)) -> Option<CachedRead> {
        let covers = |entry: &CachedRead| {
            entry.file == file
                && entry.byte_range.0 <= byte_range.0
                && entry.byte_range.1 >= byte_range.1
        };
        let mut best: Option<CachedRead> = None;
        let mut consider = |entry: CachedRead| {
            if covers(&entry) && best.as_ref().is_none_or(|b| entry.served_at > b.served_at) {
                best = Some(entry);
            }
        };
        match &self.backing {
            Backing::Null => {}
            Backing::Disk(path) => {
                for raw in Self::read_disk(path).values() {
                    if raw.get("file").and_then(|f| f.as_str()) != Some(file) {
                        continue;
                    }
                    if let Ok(entry) = serde_json::from_value::<CachedRead>(raw.clone()) {
                        consider(entry);
                    }
                }
            }
            Backing::Memory(entries) => {
                for entry in entries.lock().unwrap().iter() {
                    consider(entry.clone());
                }
            }
        }
        best
    }

    pub fn invalidate(&self, file: &str) {
        match &self.backing {
            Backing::Null => {}
            Backing::Disk(path) => {
                let mut entries = Self::read_disk(path);
                entries.retain(|_, v| v.get("file").and_then(|f| f.as_str()) != Some(file));
                Self::write_disk(path, &entries);
            }
            Backing::Memory(entries) => entries.lock().unwrap().retain(|e| e.file != file),
        }
    }
}

/// Record lines `start_line..=end_line` of `file_rel` as shown to the agent.
/// Unreadable files are skipped.
pub fn record_served(
    cache: &ReadCache,
    project_root: &Path,
    file_rel: &str,
    start_line: i64,
    end_line: i64,
) {
    use crate::writes::patch::{line_end_byte, line_start_byte, mtime_f64, now_f64, short_sha256};

    let abs = project_root.join(file_rel);
    let (Ok(data), Some(mtime)) = (std::fs::read(&abs), mtime_f64(&abs)) else {
        return;
    };
    let start = line_start_byte(&data, start_line);
    let end = line_end_byte(&data, end_line);
    cache.record(&CachedRead {
        file: file_rel.to_owned(),
        byte_range: (start, end),
        content_hash: short_sha256(data.get(start..end).unwrap_or_default()),
        served_at: now_f64(),
        served_mtime: mtime,
        tool_call_idx: 0,
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(file: &str, range: (usize, usize), served_at: f64) -> CachedRead {
        CachedRead {
            file: file.into(),
            byte_range: range,
            content_hash: String::new(),
            served_at,
            served_mtime: 1.0,
            tool_call_idx: 0,
        }
    }

    #[test]
    fn in_memory_cache_covers_records_and_invalidates() {
        let cache = ReadCache::in_memory();
        assert!(cache.find_covering("a.py", (0, 5)).is_none());
        cache.record(&entry("a.py", (0, 10), 1.0));
        cache.record(&entry("a.py", (0, 20), 2.0));
        cache.record(&entry("b.py", (0, 20), 3.0));
        let hit = cache.find_covering("a.py", (2, 8)).unwrap();
        assert_eq!(hit.byte_range, (0, 20));
        assert!(cache.find_covering("a.py", (5, 25)).is_none());
        cache.invalidate("a.py");
        assert!(cache.find_covering("a.py", (2, 8)).is_none());
        assert!(cache.find_covering("b.py", (2, 8)).is_some());
    }
}
