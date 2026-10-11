//! Port of shared/linguist/blob.py — the per-file view the detector sees.

use std::cell::OnceCell;
use std::io::Read;
use std::path::{Path, PathBuf};

use crate::config::splitext_ext;
use crate::lines::{decode_ignore, is_blank, py_splitlines};

/// Hard cap on how much of a file is ever pulled into memory (blob.py:19).
pub const MAX_READ_BYTES: u64 = 5 * 1024 * 1024;

pub struct Blob {
    pub path: PathBuf,
    pub name: String,
    /// True size from stat — NEVER content length (blob.py:36).
    pub size: u64,
    pub is_symlink: bool,
    pub truncated: bool,
    content: Vec<u8>,
    data: OnceCell<String>,
    line_ranges: OnceCell<Vec<(usize, usize)>>,
}

impl Blob {
    pub fn from_path(path: &Path, is_symlink: bool, stat_size: u64) -> std::io::Result<Blob> {
        let mut content = Vec::with_capacity(stat_size.min(MAX_READ_BYTES) as usize);
        std::fs::File::open(path)?
            .take(MAX_READ_BYTES)
            .read_to_end(&mut content)?;
        Ok(Self::from_bytes(path, content, is_symlink, stat_size))
    }

    pub fn from_bytes(path: &Path, content: Vec<u8>, is_symlink: bool, stat_size: u64) -> Blob {
        Blob {
            name: path
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default(),
            path: path.to_owned(),
            size: stat_size,
            is_symlink,
            truncated: stat_size > content.len() as u64,
            content,
            data: OnceCell::new(),
            line_ranges: OnceCell::new(),
        }
    }

    /// `bytes.decode(errors="ignore")` — lazily computed, cached.
    pub fn data(&self) -> &str {
        self.data.get_or_init(|| decode_ignore(&self.content))
    }

    /// First 1024 BYTES fail strict utf-8 → likely binary (blob.py:98).
    /// A multibyte char cut at the boundary counts as failure, same as
    /// Python decoding the raw byte slice.
    pub fn likely_binary(&self) -> bool {
        let n = self.content.len().min(1024);
        std::str::from_utf8(&self.content[..n]).is_err()
    }

    /// NUL byte anywhere in the read buffer (blob.py:118).
    pub fn binary(&self) -> bool {
        !self.content.is_empty() && memchr::memchr(0, &self.content).is_some()
    }

    pub fn empty(&self) -> bool {
        self.content.is_empty()
    }

    /// os.path.splitext extension, original case (blob.py:82).
    pub fn extname(&self) -> &str {
        splitext_ext(&self.name)
    }

    fn lines(&self) -> &[(usize, usize)] {
        self.line_ranges.get_or_init(|| py_splitlines(self.data()))
    }

    pub fn line(&self, i: usize) -> &str {
        let (a, b) = self.lines()[i];
        &self.data()[a..b]
    }

    pub fn first_lines(&self, n: usize) -> Vec<&str> {
        let count = self.lines().len().min(n);
        (0..count).map(|i| self.line(i)).collect()
    }

    pub fn last_lines(&self, n: usize) -> Vec<&str> {
        let total = self.lines().len();
        (total.saturating_sub(n)..total).map(|i| self.line(i)).collect()
    }

    /// Total physical lines (blob.py loc).
    pub fn loc(&self) -> u64 {
        self.lines().len() as u64
    }

    /// Non-blank lines (blob.py sloc).
    pub fn sloc(&self) -> u64 {
        let data = self.data();
        self.lines()
            .iter()
            .filter(|&&(a, b)| !is_blank(&data[a..b]))
            .count() as u64
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blob(content: &[u8]) -> Blob {
        Blob::from_bytes(
            Path::new("test.txt"),
            content.to_vec(),
            false,
            content.len() as u64,
        )
    }

    #[test]
    fn loc_sloc() {
        let b = blob(b"a\n\n  \nb\r\nc");
        assert_eq!(b.loc(), 5);
        assert_eq!(b.sloc(), 3);
    }

    #[test]
    fn gates() {
        assert!(blob(b"").empty());
        assert!(blob(b"a\x00b").binary());
        assert!(!blob(b"plain").binary());
        assert!(blob(&[0xff, 0xfe, 0x00, 0x01]).likely_binary());
        assert!(!blob("héllo".as_bytes()).likely_binary());
        // multibyte char split exactly at the 1024 boundary → likely_binary,
        // same artifact as Python decoding content[:1024]
        let mut v = vec![b'a'; 1023];
        v.extend("é".as_bytes()); // 2-byte char at offset 1023
        v.extend(b"tail");
        assert!(blob(&v).likely_binary());
    }

    #[test]
    fn first_last_lines() {
        let b = blob(b"1\n2\n3\n4\n5\n6\n7");
        assert_eq!(b.first_lines(2), vec!["1", "2"]);
        assert_eq!(b.last_lines(2), vec!["6", "7"]);
        assert_eq!(b.first_lines(99).len(), 7);
    }
}
