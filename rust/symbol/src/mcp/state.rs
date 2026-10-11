//! Per-server session: one project root, the in-RAM index, and the read
//! cache. The index is built once at startup, reloaded only when the file on
//! disk is newer (another process saved it), and refreshed per file after
//! every write so the next call sees current byte ranges.

use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

use anyhow::{anyhow, Result};

use crate::adapters::{default_registry, Registry};
use crate::index::{get_or_build, index_path, supported_language, Index};
use crate::writes::patch::mtime_f64;
use crate::writes::read_cache::ReadCache;

pub enum FileStatus {
    Ok(String),
    NotFound(String),
    OutsideProject,
    Unsupported(String),
}

pub struct Session {
    pub root: PathBuf,
    pub reg: &'static Registry,
    pub cache: ReadCache,
    index: Option<Index>,
    index_mtime: Option<f64>,
    pub index_source: &'static str,
}

impl Session {
    /// Build or load the index up front. A failure (e.g. a daemon that can't
    /// start) is kept as `index: None`; every tool call retries the load and
    /// reports the error.
    pub fn start(root: &Path) -> (Session, Result<()>) {
        let mut session = Session {
            root: root.to_owned(),
            reg: default_registry(root),
            cache: ReadCache::in_memory(),
            index: None,
            index_mtime: None,
            index_source: "?",
        };
        let loaded = session.load_index();
        (session, loaded)
    }

    fn disk_mtime(&self) -> Option<f64> {
        mtime_f64(&index_path(&self.root))
    }

    fn load_index(&mut self) -> Result<()> {
        let (index, source) = get_or_build(&self.root, self.reg)?;
        self.index = Some(index);
        self.index_source = source;
        self.index_mtime = self.disk_mtime();
        Ok(())
    }

    pub fn loaded_index(&self) -> Option<&Index> {
        self.index.as_ref()
    }

    /// The in-RAM index, reloaded only when the on-disk file is newer.
    pub fn index(&mut self) -> Result<&mut Index> {
        let reload = match (&self.index, self.disk_mtime()) {
            (None, _) => true,
            (Some(_), Some(disk)) => self.index_mtime.is_none_or(|cached| disk > cached),
            (Some(_), None) => false,
        };
        if reload {
            self.load_index()?;
        }
        self.index
            .as_mut()
            .ok_or_else(|| anyhow!("index not loaded"))
    }

    /// `index()` plus the read cache, borrowed together for the writes.
    pub fn index_and_cache(&mut self) -> Result<(&mut Index, &ReadCache)> {
        self.index()?;
        let index = self
            .index
            .as_mut()
            .ok_or_else(|| anyhow!("index not loaded"))?;
        Ok((index, &self.cache))
    }

    /// Adopt an index that was just built and saved (Refresh).
    pub fn replace_index(&mut self, index: Index) {
        self.index = Some(index);
        self.index_mtime = self.disk_mtime();
    }

    /// Make sure `file` (project-relative or absolute, no `:line` suffix) is
    /// in the index, indexing it on the fly when an adapter supports it.
    pub fn ensure_indexed(&mut self, file: &str) -> Result<FileStatus> {
        let p = Path::new(file);
        let abs = resolve(&if p.is_absolute() {
            p.to_owned()
        } else {
            self.root.join(p)
        });
        let Some(rel) = relative_to(&abs, &self.root) else {
            return Ok(FileStatus::OutsideProject);
        };
        if !abs.is_file() {
            return Ok(FileStatus::NotFound(rel));
        }
        let reg = self.reg;
        let index = self.index()?;
        if index.language_of_file(&rel).is_some() {
            return Ok(FileStatus::Ok(rel));
        }
        if supported_language(&abs, reg).is_none() {
            return Ok(FileStatus::Unsupported(rel));
        }
        let refreshed = index.refresh(&HashSet::from([rel.clone()]), &HashSet::new(), reg);
        self.persist(refreshed)?;
        Ok(FileStatus::Ok(rel))
    }

    /// Refresh `rels` in the in-RAM index after a successful write and save
    /// it, so other processes in the project see the new state too.
    pub fn invalidate(&mut self, rels: HashSet<String>) {
        if rels.is_empty() {
            return;
        }
        let reg = self.reg;
        let Some(index) = self.index.as_mut() else {
            return;
        };
        let refreshed = index.refresh(&rels, &HashSet::new(), reg);
        if let Err(err) = self.persist(refreshed) {
            eprintln!("[symbol-mcp] index not refreshed after write: {err:#}");
        }
    }

    /// Save after a refresh. A failed refresh leaves the in-RAM index
    /// partially tombstoned, so it is dropped rather than saved.
    fn persist(&mut self, refreshed: Result<()>) -> Result<()> {
        let saved = refreshed.and_then(|()| match &self.index {
            Some(index) => index.save().map(|_| ()),
            None => Ok(()),
        });
        match saved {
            Ok(()) => {
                if let Some(mtime) = self.disk_mtime() {
                    self.index_mtime = Some(mtime);
                }
                Ok(())
            }
            Err(err) => {
                self.index = None;
                self.index_mtime = None;
                Err(err)
            }
        }
    }
}

/// `Path.resolve()` (non-strict): symlinks resolved for the existing prefix,
/// the remainder normalized lexically.
pub fn resolve(path: &Path) -> PathBuf {
    let parts: Vec<Component> = path.components().collect();
    for split in (1..=parts.len()).rev() {
        let Ok(mut out) = parts[..split].iter().collect::<PathBuf>().canonicalize() else {
            continue;
        };
        for part in &parts[split..] {
            match part {
                Component::ParentDir => {
                    out.pop();
                }
                Component::CurDir => {}
                other => out.push(other.as_os_str()),
            }
        }
        return out;
    }
    path.to_owned()
}

/// `abs.relative_to(root)` as a `/`-separated string, the form the index
/// keys files by.
pub fn relative_to(abs: &Path, root: &Path) -> Option<String> {
    let rel = abs.strip_prefix(root).ok()?;
    let parts: Vec<String> = rel
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    Some(if parts.is_empty() {
        ".".to_owned()
    } else {
        parts.join("/")
    })
}
