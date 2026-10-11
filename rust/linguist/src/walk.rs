//! Port of linguist.py `_get_all_file_paths`: os.walk (followlinks=False)
//! with dot-DIR skipping (dot FILES are kept, faithfully), vendor.yml dir
//! and file pruning against forward-slash repo-relative paths, and exclude
//! globs. NOT gitignore-aware, by design.

use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use crate::fnmatch::GlobPat;

pub struct WalkedFile {
    pub abs: PathBuf,
    /// forward-slash repo-relative path
    pub rel: String,
    pub is_symlink: bool,
    pub mtime_ns: i64,
    pub size: u64,
}

pub struct WalkOptions<'a> {
    pub exclude: &'a [GlobPat],
    pub vendor: Option<&'a regex::Regex>,
}

pub fn walk(root: &Path, opts: &WalkOptions) -> Vec<WalkedFile> {
    let mut out = Vec::new();
    walk_dir(root, "", opts, &mut out);
    out
}

fn excluded(rel: &str, exclude: &[GlobPat]) -> bool {
    exclude.iter().any(|g| g.matches(rel))
}

fn walk_dir(dir: &Path, rel_dir: &str, opts: &WalkOptions, out: &mut Vec<WalkedFile>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut entries: Vec<_> = entries.flatten().collect();
    // os.walk order is filesystem order; sort for deterministic output.
    // Aggregate results are order-independent, so this is not a divergence.
    entries.sort_by_key(|e| e.file_name());

    for entry in entries {
        let name = entry.file_name().to_string_lossy().into_owned();
        let rel = if rel_dir.is_empty() {
            name.clone()
        } else {
            format!("{rel_dir}/{name}")
        };
        let Ok(lmeta) = entry.metadata() else { continue }; // lstat via DirEntry
        let is_symlink = lmeta.file_type().is_symlink();

        // Classify like os.walk: is_dir() FOLLOWS symlinks, but
        // followlinks=False never descends symlinked dirs — so a
        // symlink-to-dir is neither walked nor yielded.
        let followed = if is_symlink {
            std::fs::metadata(entry.path()).ok()
        } else {
            Some(lmeta.clone())
        };
        let is_dir = followed.as_ref().map(|m| m.is_dir()).unwrap_or(false);

        if is_dir {
            if is_symlink {
                continue; // in dirs, never descended, never yields
            }
            if name.starts_with('.') {
                continue;
            }
            let mut rel_slash = rel.clone();
            rel_slash.push('/');
            if let Some(v) = opts.vendor {
                if v.is_match(&rel_slash) {
                    continue;
                }
            }
            if excluded(&rel, opts.exclude) {
                continue;
            }
            walk_dir(&entry.path(), &rel, opts, out);
        } else {
            if excluded(&rel, opts.exclude) {
                continue;
            }
            if let Some(v) = opts.vendor {
                if v.is_match(&rel) {
                    continue;
                }
            }
            let meta = followed.unwrap_or(lmeta);
            let mtime_ns = meta
                .modified()
                .ok()
                .and_then(|m| m.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_nanos() as i64)
                .unwrap_or(0);
            out.push(WalkedFile {
                abs: entry.path(),
                rel,
                is_symlink,
                mtime_ns,
                size: meta.len(),
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{build_vendor_regex, load, ConfigSource};

    #[test]
    fn walk_prunes_correctly() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::create_dir_all(root.join("node_modules/pkg")).unwrap();
        std::fs::create_dir_all(root.join(".git")).unwrap();
        std::fs::write(root.join("src/main.py"), "print(1)\n").unwrap();
        std::fs::write(root.join("node_modules/pkg/x.js"), "1\n").unwrap();
        std::fs::write(root.join(".git/config"), "x\n").unwrap();
        std::fs::write(root.join(".dotfile"), "kept\n").unwrap();
        std::fs::write(root.join("skipme.log"), "x\n").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(root.join("src/main.py"), root.join("link.py")).unwrap();

        let cfg = load(&ConfigSource::Embedded).unwrap();
        let (vendor, _) = build_vendor_regex(&cfg.vendor_patterns);
        let exclude = vec![GlobPat::new("*.log").unwrap()];
        let files = walk(
            root,
            &WalkOptions {
                exclude: &exclude,
                vendor: vendor.as_ref(),
            },
        );
        let rels: Vec<&str> = files.iter().map(|f| f.rel.as_str()).collect();
        assert!(rels.contains(&"src/main.py"));
        assert!(rels.contains(&".dotfile"), "dot FILES are kept: {rels:?}");
        assert!(!rels.iter().any(|r| r.contains("node_modules")), "{rels:?}");
        assert!(!rels.iter().any(|r| r.starts_with(".git/")));
        assert!(!rels.contains(&"skipme.log"));
        #[cfg(unix)]
        {
            let link = files.iter().find(|f| f.rel == "link.py").unwrap();
            assert!(link.is_symlink);
        }
    }
}
