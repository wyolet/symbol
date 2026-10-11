//! Persisted classification cache — `.symbol/linguist.msgpack.zst`.
//! Re-classifies only changed files; persisted as msgpack+zstd with atomic writes.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const CACHE_VERSION: u32 = 1;
/// Bump when classification/counting logic changes in a way that invalidates
/// cached results.
pub const CACHE_LOGIC_REV: u32 = 1;
pub const ZSTD_LEVEL: i32 = 3;

const CACHE_REL_PATH: &str = ".symbol/linguist.msgpack.zst";

/// Per-file record. `lang: None` = detect() gated the file out
/// (binary/empty/symlink) — negative results are cached so binaries aren't
/// re-read every run. The language is stored by NAME (stable across runs;
/// config changes are covered by config_fp).
#[derive(Serialize, Deserialize)]
pub struct FileRec(
    pub i64,            // mtime_ns
    pub u64,            // size
    pub Option<String>, // language name
    pub u64,            // loc
    pub u64,            // sloc
);

#[derive(Serialize, Deserialize)]
pub struct CacheFile {
    pub v: u32,
    pub root: String,
    pub config_fp: u64,
    pub files: HashMap<String, FileRec>,
}

pub struct Cache {
    path: PathBuf,
    root: String,
    config_fp: u64,
    pub loaded: HashMap<String, FileRec>,
    pub hits: usize,
    pub misses: usize,
}

/// config fingerprint + logic rev + heuristics flag → one u64.
pub fn effective_fingerprint(config_fp: u64, heuristics_enabled: bool) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    config_fp.hash(&mut h);
    CACHE_LOGIC_REV.hash(&mut h);
    heuristics_enabled.hash(&mut h);
    h.finish()
}

impl Cache {
    /// Load, or start empty on ANY problem (missing, corrupt, version/root/
    /// fingerprint mismatch) — the cache must never fail a run.
    pub fn load(root: &Path, config_fp: u64) -> Cache {
        let root_str = root.to_string_lossy().into_owned();
        let path = root.join(CACHE_REL_PATH);
        let loaded = Self::try_read(&path)
            .filter(|c| c.v == CACHE_VERSION && c.root == root_str && c.config_fp == config_fp)
            .map(|c| c.files)
            .unwrap_or_default();
        Cache {
            path,
            root: root_str,
            config_fp,
            loaded,
            hits: 0,
            misses: 0,
        }
    }

    fn try_read(path: &Path) -> Option<CacheFile> {
        let compressed = std::fs::read(path).ok()?;
        let raw = zstd::decode_all(compressed.as_slice()).ok()?;
        rmp_serde::from_slice(&raw).ok()
    }

    /// Cache hit: same mtime_ns AND size (blob.py's staleness would be
    /// mtime-only; size guards against mtime-preserving rewrites).
    pub fn get(&mut self, rel: &str, mtime_ns: i64, size: u64) -> Option<&FileRec> {
        match self.loaded.get(rel) {
            Some(rec) if rec.0 == mtime_ns && rec.1 == size => {
                self.hits += 1;
                self.loaded.get(rel)
            }
            _ => {
                self.misses += 1;
                None
            }
        }
    }

    /// Atomic save of exactly the given records (paths not walked this run
    /// are dropped by construction — caller passes the fresh map).
    pub fn save(&self, files: HashMap<String, FileRec>) -> std::io::Result<()> {
        let cache = CacheFile {
            v: CACHE_VERSION,
            root: self.root.clone(),
            config_fp: self.config_fp,
            files,
        };
        let raw = rmp_serde::to_vec(&cache)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        let compressed = zstd::encode_all(raw.as_slice(), ZSTD_LEVEL)?;
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = self
            .path
            .with_extension(format!("tmp.{}", std::process::id()));
        std::fs::write(&tmp, &compressed)?;
        std::fs::rename(&tmp, &self.path)
    }
}
