//! Port of shared/symbol_index.py — the columnar symbol lookup table.
//! Persisted format is BYTE-COMPATIBLE with Python's v6 index
//! (.symbol/symbol_index.msgpack.zst): either side can read the other's.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use symbol_linguist::blob::Blob;
use symbol_linguist::config as lcfg;
use symbol_linguist::detector::Detector;
use symbol_linguist::fnmatch::GlobPat;
use symbol_linguist::language::Registry as LangRegistry;
use symbol_linguist::walk::{walk, WalkOptions};

use crate::adapters::{Registry, ScannedSymbol};

pub const INDEX_VERSION: i64 = 6;
const INDEX_REL_PATH: &str = ".symbol/symbol_index.msgpack.zst";
const MAX_INDEX_FILE_BYTES: u64 = 20 * 1024 * 1024;
const ZSTD_LEVEL: i32 = 3;
const REBUILD_THRESHOLD: usize = 200;
const COMPACT_RATIO: f64 = 0.30;
const COMPACT_MIN: i64 = 100;

// Symbol row positions (shared/symbol.py).
pub const S_PATH: usize = 0;
pub const S_FILE: usize = 1;
pub const S_SLINE: usize = 2;
pub const S_ELINE: usize = 3;
pub const S_SBYTE: usize = 4;
pub const S_EBYTE: usize = 5;
pub const S_KIND: usize = 6;
pub const S_LANG: usize = 7;
pub const S_PARENT: usize = 8;

pub const REF_NAME: i64 = 0;
pub const REF_ATTR: i64 = 1;

pub fn ref_label(kind: i64) -> &'static str {
    if kind == REF_ATTR {
        "attr"
    } else {
        "name"
    }
}

fn pack_ref(name_id: i64, kind: i64) -> i64 {
    (name_id << 1) | (kind & 1)
}

fn unpack_ref(packed: i64) -> (i64, i64) {
    (packed >> 1, packed & 1)
}

// ---------------------------------------------------------- persisted payload

#[derive(Serialize, Deserialize)]
struct Payload {
    v: i64,
    root: String,
    strings: Vec<String>,
    files: Vec<String>,
    file_langs: Vec<i64>,
    kinds: Vec<String>,
    langs: Vec<String>,
    symbols: Vec<[i64; 9]>,
    imports: Vec<[i64; 4]>,
    refs_of: Vec<Vec<(i64, i64)>>,
    by_path: HashMap<String, Vec<i64>>,
    by_file: HashMap<i64, Vec<i64>>,
    imps_by_file: HashMap<i64, Vec<i64>>,
    mtimes: HashMap<String, f64>,
    dir_mtimes: HashMap<String, f64>,
    #[serde(default)]
    git_head: String,
    #[serde(default)]
    scope_include: Vec<String>,
    #[serde(default)]
    scope_exclude: Vec<String>,
    #[serde(default)]
    dead_rows: i64,
}

// ---------------------------------------------------------- linguist context

struct LinguistCtx {
    detector: Detector,
    vendor: Option<regex::Regex>,
}

static LINGUIST: OnceLock<LinguistCtx> = OnceLock::new();

fn linguist() -> &'static LinguistCtx {
    LINGUIST.get_or_init(|| {
        let configs = lcfg::load(&lcfg::ConfigSource::resolve(None)).expect("linguist configs");
        let reg = LangRegistry::load(&configs).expect("language registry");
        let (vendor, _) = lcfg::build_vendor_regex(&configs.vendor_patterns);
        // Heuristics OFF: classification identical to the Python chain, so a
        // Rust-built index gates files exactly like a Python-built one.
        LinguistCtx {
            detector: Detector::new(&configs, reg, false),
            vendor,
        }
    })
}

/// Classify one file the way ASTCache/_git_tracked_sources does:
/// linguist detect → lang.key (name lowercased), None for Unknown/gated.
fn classify(path: &Path) -> Option<String> {
    let meta = std::fs::metadata(path).ok()?;
    let blob = Blob::from_path(path, false, meta.len()).ok()?;
    let ctx = linguist();
    let id = ctx.detector.detect(&blob)?;
    let lang = ctx.detector.reg.get(id);
    if lang.name == "Unknown" {
        return None;
    }
    Some(lang.key())
}

// ---------------------------------------------------------- index

pub struct Index {
    pub project_root: PathBuf,
    pub strings: Vec<String>,
    str_id: HashMap<String, i64>,
    pub files: Vec<String>,
    file_ids: HashMap<String, i64>,
    pub file_langs: Vec<i64>,
    pub kinds: Vec<String>,
    kind_ids: HashMap<String, i64>,
    pub langs: Vec<String>,
    lang_ids: HashMap<String, i64>,
    pub symbols: Vec<[i64; 9]>,
    pub imports: Vec<[i64; 4]>,
    pub refs_of: Vec<Vec<(i64, i64)>>,
    pub by_path: HashMap<String, Vec<i64>>,
    pub by_file: HashMap<i64, Vec<i64>>,
    pub imps_by_file: HashMap<i64, Vec<i64>>,
    by_name_id: HashMap<i64, Vec<(i64, i64, i64)>>,
    saved_mtimes: HashMap<String, f64>,
    saved_dir_mtimes: HashMap<String, f64>,
    git_head: String,
    scope_include: Vec<String>,
    scope_exclude: Vec<String>,
    dead_rows: i64,
}

impl Index {
    pub fn empty(project_root: &Path) -> Index {
        Index {
            project_root: project_root.to_owned(),
            strings: vec![String::new()],
            str_id: HashMap::from([(String::new(), 0)]),
            files: vec![],
            file_ids: HashMap::new(),
            file_langs: vec![],
            kinds: vec![],
            kind_ids: HashMap::new(),
            langs: vec![],
            lang_ids: HashMap::new(),
            symbols: vec![],
            imports: vec![],
            refs_of: vec![],
            by_path: HashMap::new(),
            by_file: HashMap::new(),
            imps_by_file: HashMap::new(),
            by_name_id: HashMap::new(),
            saved_mtimes: HashMap::new(),
            saved_dir_mtimes: HashMap::new(),
            git_head: String::new(),
            scope_include: vec![],
            scope_exclude: vec![],
            dead_rows: 0,
        }
    }

    // ------------------------------------------------------ interning

    fn intern(&mut self, s: &str) -> i64 {
        if let Some(&id) = self.str_id.get(s) {
            return id;
        }
        let id = self.strings.len() as i64;
        self.strings.push(s.to_owned());
        self.str_id.insert(s.to_owned(), id);
        id
    }

    fn kind_id(&mut self, k: &str) -> i64 {
        if let Some(&id) = self.kind_ids.get(k) {
            return id;
        }
        let id = self.kinds.len() as i64;
        self.kinds.push(k.to_owned());
        self.kind_ids.insert(k.to_owned(), id);
        id
    }

    fn lang_id(&mut self, l: &str) -> i64 {
        if let Some(&id) = self.lang_ids.get(l) {
            return id;
        }
        let id = self.langs.len() as i64;
        self.langs.push(l.to_owned());
        self.lang_ids.insert(l.to_owned(), id);
        id
    }

    fn file_id(&mut self, rel: &str, language: &str) -> i64 {
        if let Some(&id) = self.file_ids.get(rel) {
            return id;
        }
        let id = self.files.len() as i64;
        self.files.push(rel.to_owned());
        self.file_ids.insert(rel.to_owned(), id);
        let lid = self.lang_id(language);
        self.file_langs.push(lid);
        id
    }

    // ------------------------------------------------------ build

    /// _walk_file: ingest one file via its language adapter.
    pub fn walk_file(&mut self, path: &Path, language: Option<&str>, reg: &Registry) {
        let Ok(rel) = path.strip_prefix(&self.project_root) else {
            return;
        };
        let rel = rel.to_string_lossy().replace(std::path::MAIN_SEPARATOR, "/");
        let Ok(meta) = std::fs::metadata(path) else {
            return;
        };
        if meta.len() > MAX_INDEX_FILE_BYTES {
            return;
        }
        let language = match language {
            Some(l) => l.to_owned(),
            None => match classify(path) {
                Some(l) => l,
                None => return,
            },
        };
        if !reg.has_adapter(&language) {
            return;
        }
        let Ok(adapter) = reg.for_language(&language) else {
            return;
        };
        let Ok(scan) = adapter.scan_file(path, None) else {
            return;
        };
        if !scan.ok {
            return;
        }

        let file_id = self.file_id(&rel, &scan.language);
        let lang_id = self.lang_id(&scan.language);

        for imp in &scan.imports {
            let imp_idx = self.imports.len() as i64;
            let name_id = self.intern(&imp.local);
            let source_id = self.intern(&imp.source);
            self.imports.push([file_id, name_id, source_id, imp.line]);
            self.imps_by_file.entry(file_id).or_default().push(imp_idx);
        }
        for top in &scan.symbols {
            self.ingest_symbol(top, file_id, -1, lang_id);
        }
    }

    fn ingest_symbol(&mut self, sym: &ScannedSymbol, file_id: i64, parent_row: i64, lang_id: i64) {
        let row_idx = self.symbols.len() as i64;
        let path_id = self.intern(&sym.qualified_path);
        let kind_id = self.kind_id(&sym.kind);
        self.symbols.push([
            path_id,
            file_id,
            sym.line_range.0,
            sym.line_range.1,
            sym.byte_range.0,
            sym.byte_range.1,
            kind_id,
            lang_id,
            parent_row,
        ]);
        let mut packed = Vec::with_capacity(sym.refs.len());
        for r in &sym.refs {
            let kind_bit = if r.kind == "name" { REF_NAME } else { REF_ATTR };
            let name_id = self.intern(&r.name);
            packed.push((pack_ref(name_id, kind_bit), r.line));
        }
        self.refs_of.push(packed);
        self.by_path
            .entry(sym.qualified_path.clone())
            .or_default()
            .push(row_idx);
        self.by_file.entry(file_id).or_default().push(row_idx);
        for child in &sym.children {
            self.ingest_symbol(child, file_id, row_idx, lang_id);
        }
    }

    pub fn build_by_name_id(&mut self) {
        let mut by: HashMap<i64, Vec<(i64, i64, i64)>> = HashMap::new();
        for (src_row, refs) in self.refs_of.iter().enumerate() {
            for &(packed, line) in refs {
                let (name_id, kind) = unpack_ref(packed);
                by.entry(name_id).or_default().push((src_row as i64, line, kind));
            }
        }
        self.by_name_id = by;
    }

    // ------------------------------------------------------ accessors

    pub fn num_refs(&self) -> usize {
        self.refs_of.iter().map(Vec::len).sum()
    }

    pub fn path_of(&self, row: i64) -> &str {
        &self.strings[self.symbols[row as usize][S_PATH] as usize]
    }

    pub fn kind_of(&self, row: i64) -> &str {
        &self.kinds[self.symbols[row as usize][S_KIND] as usize]
    }

    pub fn language_of(&self, row: i64) -> &str {
        &self.langs[self.symbols[row as usize][S_LANG] as usize]
    }

    pub fn range_of(&self, row: i64) -> (i64, i64) {
        let s = &self.symbols[row as usize];
        (s[S_SLINE], s[S_ELINE])
    }

    pub fn byte_range_of(&self, row: i64) -> (i64, i64) {
        let s = &self.symbols[row as usize];
        (s[S_SBYTE], s[S_EBYTE])
    }

    pub fn file_of(&self, row: i64) -> &str {
        &self.files[self.symbols[row as usize][S_FILE] as usize]
    }

    pub fn file_id_of(&self, rel: &str) -> Option<i64> {
        self.file_ids.get(rel).copied()
    }

    pub fn language_of_file(&self, rel: &str) -> Option<&str> {
        let fid = *self.file_ids.get(rel)?;
        Some(&self.langs[self.file_langs[fid as usize] as usize])
    }

    pub fn parent_of(&self, row: i64) -> i64 {
        self.symbols[row as usize][S_PARENT]
    }

    /// (name, source, line) per import binding in this file.
    pub fn imports_for(&self, file_id: i64) -> Vec<(&str, &str, i64)> {
        self.imps_by_file
            .get(&file_id)
            .map(|idxs| {
                idxs.iter()
                    .map(|&i| {
                        let imp = &self.imports[i as usize];
                        (
                            self.strings[imp[1] as usize].as_str(),
                            self.strings[imp[2] as usize].as_str(),
                            imp[3],
                        )
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// (name, kind_label, line) per ref of a symbol row.
    pub fn refs_for(&self, row: i64) -> Vec<(&str, &'static str, i64)> {
        self.refs_of[row as usize]
            .iter()
            .map(|&(packed, line)| {
                let (name_id, kind) = unpack_ref(packed);
                (self.strings[name_id as usize].as_str(), ref_label(kind), line)
            })
            .collect()
    }

    /// (source_row, line, kind_label) for every ref matching `name`.
    pub fn callers_of(&self, name: &str) -> Vec<(i64, i64, &'static str)> {
        let Some(&nid) = self.str_id.get(name) else {
            return vec![];
        };
        self.by_name_id
            .get(&nid)
            .map(|hits| {
                hits.iter()
                    .map(|&(src, line, kind)| (src, line, ref_label(kind)))
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn body(&self, row: i64) -> String {
        let s = &self.symbols[row as usize];
        let abs = self.project_root.join(&self.files[s[S_FILE] as usize]);
        match std::fs::read(&abs) {
            Ok(data) => {
                let start = (s[S_SBYTE] as usize).min(data.len());
                let end = (s[S_EBYTE] as usize).min(data.len());
                String::from_utf8_lossy(&data[start..end]).into_owned()
            }
            Err(_) => String::new(),
        }
    }

    /// Declaration line(s). Delegates to the row's language adapter, exactly
    /// like Python; falls back to the first line when no adapter.
    pub fn signature(&self, row: i64, reg: &Registry) -> String {
        let s = &self.symbols[row as usize];
        let abs = self.project_root.join(&self.files[s[S_FILE] as usize]);
        let span = ((s[S_EBYTE] - s[S_SBYTE]).max(0) as usize).min(4096);
        let text = match std::fs::read(&abs) {
            Ok(data) => {
                let start = (s[S_SBYTE] as usize).min(data.len());
                let end = (start + span).min(data.len());
                String::from_utf8_lossy(&data[start..end]).into_owned()
            }
            Err(_) => return String::new(),
        };
        let language = self.language_of(row).to_owned();
        if let Ok(adapter) = reg.for_language(&language) {
            if let Ok(sig) = adapter.signature(&text) {
                return sig;
            }
        }
        text.lines().next().unwrap_or("").trim().to_owned()
    }

    pub fn descendants_of(&self, root: i64) -> Vec<i64> {
        let mut out = vec![];
        let mut stack = vec![root];
        let mut seen = std::collections::HashSet::from([root]);
        while let Some(parent) = stack.pop() {
            for (i, sym) in self.symbols.iter().enumerate() {
                let i = i as i64;
                if sym[S_PARENT] == parent && seen.insert(i) {
                    out.push(i);
                    stack.push(i);
                }
            }
        }
        out
    }

    pub fn build_tree(&self, row_ids: &[i64], root_row: Option<i64>, reg: &Registry) -> Vec<serde_json::Value> {
        let mut nodes: HashMap<i64, serde_json::Value> = HashMap::new();
        let mut order: Vec<i64> = vec![];
        for &row in row_ids {
            let (s, e) = self.range_of(row);
            nodes.insert(
                row,
                serde_json::json!({
                    "path": self.path_of(row),
                    "kind": self.kind_of(row),
                    "signature": self.signature(row, reg),
                    "start_line": s,
                    "end_line": e,
                    "children": [],
                }),
            );
            order.push(row);
        }
        // Attach children to parents bottom-up by processing in reverse
        // insertion order is unnecessary: replicate Python's single pass —
        // parents appear before children in row_ids (sorted by line).
        let mut roots: Vec<serde_json::Value> = vec![];
        let mut placed_into: HashMap<i64, Vec<serde_json::Value>> = HashMap::new();
        // Collect children lists first.
        for &row in order.iter().rev() {
            let node = {
                let mut n = nodes.remove(&row).unwrap();
                if let Some(kids) = placed_into.remove(&row) {
                    let arr = n["children"].as_array_mut().unwrap();
                    for k in kids.into_iter().rev() {
                        arr.push(k);
                    }
                }
                n
            };
            let parent = self.parent_of(row);
            let is_root = Some(row) == root_row || !order.contains(&parent);
            if is_root {
                roots.push(node);
            } else {
                placed_into.entry(parent).or_default().push(node);
            }
        }
        roots.reverse();
        roots
    }

    /// Full payload for a symbol row: body + imports + refs, deduped.
    pub fn row_payload(&self, row: i64, reg: &Registry) -> serde_json::Value {
        let (s, e) = self.range_of(row);
        let raw_refs = self.refs_for(row);

        let mut ref_order: Vec<(String, String)> = vec![];
        let mut ref_index: HashMap<(String, String), serde_json::Value> = HashMap::new();
        for (n, k, ln) in &raw_refs {
            let key = (n.to_string(), k.to_string());
            match ref_index.get_mut(&key) {
                Some(existing) => {
                    existing["lines"].as_array_mut().unwrap().push((*ln).into());
                }
                None => {
                    ref_order.push(key.clone());
                    ref_index.insert(
                        key,
                        serde_json::json!({"name": n, "kind": k, "line": ln, "lines": [ln]}),
                    );
                }
            }
        }
        let used_names: std::collections::HashSet<&str> = raw_refs
            .iter()
            .filter(|(_, k, _)| *k == "name")
            .map(|(n, _, _)| *n)
            .collect();
        let file_id = self.symbols[row as usize][S_FILE];
        let imports: Vec<serde_json::Value> = self
            .imports_for(file_id)
            .into_iter()
            .filter(|(n, _, _)| used_names.contains(n))
            .map(|(n, src, ln)| serde_json::json!({"name": n, "source": src, "line": ln}))
            .collect();

        serde_json::json!({
            "path": self.path_of(row),
            "file": self.file_of(row),
            "start_line": s,
            "end_line": e,
            "kind": self.kind_of(row),
            "language": self.language_of(row),
            "signature": self.signature(row, reg),
            "body": self.body(row),
            "imports": imports,
            "refs": ref_order.into_iter().map(|k| ref_index.remove(&k).unwrap()).collect::<Vec<_>>(),
        })
    }

    pub fn raw_slice(&self, file: &str, start: i64, end: i64) -> serde_json::Value {
        let abs = self.project_root.join(file);
        let body = std::fs::read(&abs)
            .map(|data| {
                let text = String::from_utf8_lossy(&data).into_owned();
                let lines: Vec<&str> = text.lines().collect();
                let s = ((start - 1).max(0) as usize).min(lines.len());
                let e = (end as usize).min(lines.len());
                lines[s..e].join("\n")
            })
            .unwrap_or_default();
        serde_json::json!({
            "path": null,
            "file": file,
            "start_line": start,
            "end_line": end,
            "kind": "slice",
            "language": self.language_of_file(file),
            "signature": null,
            "body": body,
            "imports": [],
            "refs": [],
        })
    }

    pub fn stats(&self) -> serde_json::Value {
        serde_json::json!({
            "files": self.files.len(),
            "symbols": self.symbols.len(),
            "live_symbols": self.symbols.len() as i64 - self.dead_rows,
            "dead_rows": self.dead_rows,
            "imports": self.imports.len(),
            "refs": self.num_refs(),
            "strings": self.strings.len(),
        })
    }

    // ------------------------------------------------------ staleness

    fn snapshot_mtimes(&self) -> HashMap<String, f64> {
        self.files
            .iter()
            .map(|rel| (rel.clone(), mtime_f64(&self.project_root.join(rel))))
            .collect()
    }

    fn snapshot_dir_mtimes(&self) -> HashMap<String, f64> {
        let mut dirs: std::collections::HashSet<String> =
            std::collections::HashSet::from([".".to_owned()]);
        for rel in &self.files {
            if let Some(parent) = Path::new(rel).parent() {
                let p = parent.to_string_lossy();
                if !p.is_empty() && p != "." {
                    dirs.insert(p.into_owned());
                }
            }
        }
        dirs.into_iter()
            .map(|d| {
                let mt = mtime_f64(&self.project_root.join(&d));
                (d, mt)
            })
            .collect()
    }

    fn current_head(&self) -> String {
        git(&self.project_root, &["rev-parse", "HEAD"])
            .map(|o| o.trim().to_owned())
            .unwrap_or_default()
    }

    fn git_commit_changes(&self, from_sha: &str) -> std::collections::HashSet<String> {
        if from_sha.is_empty() {
            return Default::default();
        }
        git(
            &self.project_root,
            &["diff", "--name-only", &format!("{from_sha}..HEAD")],
        )
        .map(|out| out.lines().filter(|l| !l.is_empty()).map(str::to_owned).collect())
        .unwrap_or_default()
    }

    fn git_tracked_sources(&self, reg: &Registry) -> Option<std::collections::HashSet<String>> {
        let tracked = git_bytes(&self.project_root, &["ls-files", "-z"])?;
        let untracked = git_bytes(
            &self.project_root,
            &["ls-files", "-z", "--others", "--exclude-standard"],
        )?;
        let mut rels: std::collections::HashSet<String> = Default::default();
        for chunk in tracked.split(|&b| b == 0).chain(untracked.split(|&b| b == 0)) {
            if !chunk.is_empty() {
                rels.insert(String::from_utf8_lossy(chunk).into_owned());
            }
        }
        let mut out = std::collections::HashSet::new();
        for rel in rels {
            let abs = self.project_root.join(&rel);
            if let Some(key) = classify(&abs) {
                if reg.has_adapter(&key) {
                    out.insert(rel);
                }
            }
        }
        Some(out)
    }

    fn in_scope(&self, rel: &str) -> bool {
        let matches = |pats: &[String]| {
            pats.iter()
                .filter_map(|p| GlobPat::new(p))
                .any(|g| g.matches(rel))
        };
        if !self.scope_exclude.is_empty() && matches(&self.scope_exclude) {
            return false;
        }
        if !self.scope_include.is_empty() && !matches(&self.scope_include) {
            return false;
        }
        true
    }

    pub fn stale_files(
        &self,
        reg: &Registry,
    ) -> (std::collections::HashSet<String>, std::collections::HashSet<String>) {
        let saved: std::collections::HashSet<String> = self.files.iter().cloned().collect();
        let mut stale: std::collections::HashSet<String> = Default::default();

        let current_head = self.current_head();
        if !self.git_head.is_empty() && !current_head.is_empty() && self.git_head != current_head {
            stale.extend(self.git_commit_changes(&self.git_head));
        }

        for rel in &saved {
            let saved_mt = self.saved_mtimes.get(rel).copied().unwrap_or(0.0);
            let current = mtime_f64_checked(&self.project_root.join(rel));
            match current {
                Some(mt) if mt == saved_mt => {}
                _ => {
                    stale.insert(rel.clone());
                }
            }
        }

        let Some(current) = self.git_tracked_sources(reg) else {
            let stale: std::collections::HashSet<String> =
                stale.intersection(&saved).cloned().collect();
            return (stale, Default::default());
        };
        let current: std::collections::HashSet<String> =
            current.into_iter().filter(|p| self.in_scope(p)).collect();
        let deleted: std::collections::HashSet<String> =
            saved.difference(&current).cloned().collect();
        stale.extend(current.difference(&saved).cloned());
        let stale = stale.intersection(&current).cloned().collect();
        (stale, deleted)
    }

    pub fn quick_is_clean(&self) -> bool {
        for (rel, &saved_mt) in &self.saved_mtimes {
            match mtime_f64_checked(&self.project_root.join(rel)) {
                Some(mt) if mt == saved_mt => {}
                _ => return false,
            }
        }
        for (d, &saved_mt) in &self.saved_dir_mtimes {
            match mtime_f64_checked(&self.project_root.join(d)) {
                Some(mt) if mt == saved_mt => {}
                _ => return false,
            }
        }
        true
    }

    /// ensure_fresh: refresh `rel` in place if its mtime differs. Returns
    /// true when a refresh happened.
    pub fn ensure_fresh(&mut self, rel: &str, reg: &Registry) -> bool {
        let Some(current) = mtime_f64_checked(&self.project_root.join(rel)) else {
            return false;
        };
        if self.saved_mtimes.get(rel) == Some(&current) {
            return false;
        }
        self.refresh(
            &std::collections::HashSet::from([rel.to_owned()]),
            &Default::default(),
            reg,
        );
        true
    }

    // ------------------------------------------------------ refresh

    pub fn refresh(
        &mut self,
        stale: &std::collections::HashSet<String>,
        deleted: &std::collections::HashSet<String>,
        reg: &Registry,
    ) {
        for rel in deleted.iter().chain(stale.iter()) {
            self.tombstone_file(rel);
        }
        for rel in stale {
            let path = self.project_root.join(rel);
            if !path.exists() {
                continue;
            }
            self.walk_file(&path, None, reg);
        }
        self.build_by_name_id();
        self.saved_mtimes = self.snapshot_mtimes();
        self.saved_dir_mtimes = self.snapshot_dir_mtimes();
        self.git_head = self.current_head();
    }

    fn tombstone_file(&mut self, rel: &str) {
        let Some(&fid) = self.file_ids.get(rel) else {
            return;
        };
        let dead_rows: Vec<i64> = self.by_file.get(&fid).cloned().unwrap_or_default();
        for &row in &dead_rows {
            let old = self.symbols[row as usize];
            let path = if old[S_PATH] != 0 {
                self.strings[old[S_PATH] as usize].clone()
            } else {
                String::new()
            };
            self.symbols[row as usize] = [0, fid, 0, 0, 0, 0, 0, 0, -1];
            self.refs_of[row as usize] = vec![];
            if !path.is_empty() {
                if let Some(rows) = self.by_path.get_mut(&path) {
                    rows.retain(|&r| r != row);
                    if rows.is_empty() {
                        self.by_path.remove(&path);
                    }
                }
            }
        }
        self.dead_rows += dead_rows.len() as i64;
        self.by_file.remove(&fid);

        let dead_imps: Vec<i64> = self.imps_by_file.get(&fid).cloned().unwrap_or_default();
        for i in dead_imps {
            self.imports[i as usize] = [fid, 0, 0, 0];
        }
        self.imps_by_file.remove(&fid);
        self.saved_mtimes.remove(rel);
    }

    // ------------------------------------------------------ persistence

    pub fn save(&self) -> Result<PathBuf> {
        let target = self.project_root.join(INDEX_REL_PATH);
        if let Some(dir) = target.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let payload = Payload {
            v: INDEX_VERSION,
            root: self.project_root.to_string_lossy().into_owned(),
            strings: self.strings.clone(),
            files: self.files.clone(),
            file_langs: self.file_langs.clone(),
            kinds: self.kinds.clone(),
            langs: self.langs.clone(),
            symbols: self.symbols.clone(),
            imports: self.imports.clone(),
            refs_of: self.refs_of.clone(),
            by_path: self.by_path.clone(),
            by_file: self.by_file.clone(),
            imps_by_file: self.imps_by_file.clone(),
            mtimes: self.snapshot_mtimes(),
            dir_mtimes: self.snapshot_dir_mtimes(),
            git_head: self.current_head(),
            scope_include: self.scope_include.clone(),
            scope_exclude: self.scope_exclude.clone(),
            dead_rows: self.dead_rows,
        };
        // to_vec_named: struct → msgpack MAP with string keys, matching
        // Python's dict payload (positional arrays would not interop).
        let packed = rmp_serde::to_vec_named(&payload).context("packing index")?;
        // bulk::compress pledges the content size in the frame header —
        // Python's zstandard.decompress() refuses frames without it.
        let compressed = zstd::bulk::compress(&packed, ZSTD_LEVEL)?;
        let tmp = target.with_extension(format!("tmp.{}", std::process::id()));
        std::fs::write(&tmp, &compressed)?;
        std::fs::rename(&tmp, &target)?;
        Ok(target)
    }

    pub fn load(project_root: &Path) -> Option<Index> {
        let target = project_root.join(INDEX_REL_PATH);
        let raw = std::fs::read(target).ok()?;
        let packed = zstd::decode_all(raw.as_slice()).ok()?;
        let payload: Payload = rmp_serde::from_slice(&packed).ok()?;
        if payload.v != INDEX_VERSION {
            return None;
        }
        if payload.root != project_root.to_string_lossy() {
            return None;
        }
        let mut idx = Index::empty(project_root);
        idx.saved_mtimes = payload.mtimes;
        idx.saved_dir_mtimes = payload.dir_mtimes;
        idx.git_head = payload.git_head;
        idx.scope_include = payload.scope_include;
        idx.scope_exclude = payload.scope_exclude;
        idx.dead_rows = payload.dead_rows;
        idx.strings = payload.strings;
        idx.str_id = idx
            .strings
            .iter()
            .enumerate()
            .map(|(i, s)| (s.clone(), i as i64))
            .collect();
        idx.files = payload.files;
        idx.file_ids = idx
            .files
            .iter()
            .enumerate()
            .map(|(i, f)| (f.clone(), i as i64))
            .collect();
        idx.file_langs = payload.file_langs;
        idx.kinds = payload.kinds;
        idx.kind_ids = idx
            .kinds
            .iter()
            .enumerate()
            .map(|(i, k)| (k.clone(), i as i64))
            .collect();
        idx.langs = payload.langs;
        idx.lang_ids = idx
            .langs
            .iter()
            .enumerate()
            .map(|(i, l)| (l.clone(), i as i64))
            .collect();
        idx.symbols = payload.symbols;
        idx.imports = payload.imports;
        idx.refs_of = payload.refs_of;
        idx.by_path = payload.by_path;
        idx.by_file = payload.by_file;
        idx.imps_by_file = payload.imps_by_file;
        idx.build_by_name_id();
        Some(idx)
    }
}

// ---------------------------------------------------------- build / refresh drivers

/// The `[checker] exclude` scope Python's full build applies via
/// filter_paths: global data/spec.toml baseline + the target project's
/// `symbol.toml` / `pyproject.toml [tool.symbol.checker]` extensions.
/// (Per-package spec excludes are not shimmed yet — documented.)
fn checker_excludes(project_root: &Path) -> Vec<GlobPat> {
    const EMBEDDED_SPEC: &str =
        include_str!("../../../src/wyolet/symbol/data/spec.toml");

    let mut patterns: Vec<String> = vec![];
    let global = std::env::var("SYMBOL_REPO")
        .map(|r| Path::new(&r).join("src/wyolet/symbol/data/spec.toml"))
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .unwrap_or_else(|| EMBEDDED_SPEC.to_owned());
    if let Ok(spec) = global.parse::<toml::Table>() {
        push_excludes(&mut patterns, spec.get("checker"));
    }
    if let Ok(text) = std::fs::read_to_string(project_root.join("symbol.toml")) {
        if let Ok(cfg) = text.parse::<toml::Table>() {
            push_excludes(&mut patterns, cfg.get("checker"));
        }
    }
    if let Ok(text) = std::fs::read_to_string(project_root.join("pyproject.toml")) {
        if let Ok(cfg) = text.parse::<toml::Table>() {
            let checker = cfg
                .get("tool")
                .and_then(|t| t.get("symbol"))
                .and_then(|s| s.get("checker"));
            push_excludes(&mut patterns, checker);
        }
    }
    patterns.iter().filter_map(|p| GlobPat::new(p)).collect()
}

fn push_excludes(out: &mut Vec<String>, checker: Option<&toml::Value>) {
    if let Some(arr) = checker.and_then(|c| c.get("exclude")).and_then(|e| e.as_array()) {
        out.extend(arr.iter().filter_map(|v| v.as_str().map(str::to_owned)));
    }
}

pub fn full_build(project_root: &Path, reg: &Registry) -> Result<Index> {
    let ctx = linguist();
    let mut idx = Index::empty(project_root);
    let excludes = checker_excludes(project_root);
    let mut files = walk(
        project_root,
        &WalkOptions {
            exclude: &[],
            vendor: ctx.vendor.as_ref(),
        },
    );
    files.sort_by(|a, b| a.rel.cmp(&b.rel));
    for f in files {
        if f.is_symlink {
            continue;
        }
        // filter_paths semantics: [checker] excludes gate the full build.
        if excludes.iter().any(|g| g.matches(&f.rel)) {
            continue;
        }
        let Some(key) = classify(&f.abs) else { continue };
        if !reg.has_adapter(&key) {
            continue;
        }
        idx.walk_file(&f.abs, Some(&key), reg);
    }
    idx.build_by_name_id();
    Ok(idx)
}

/// get_or_build_index: load, refresh incrementally, or rebuild.
/// Returns (index, "disk" | "refresh" | "rebuild" | "build" | "compact").
pub fn get_or_build(project_root: &Path, reg: &Registry) -> Result<(Index, &'static str)> {
    let Some(mut existing) = Index::load(project_root) else {
        let idx = full_build(project_root, reg)?;
        idx.save()?;
        return Ok((idx, "build"));
    };

    if existing.quick_is_clean() {
        return Ok((existing, "disk"));
    }
    let (stale, deleted) = existing.stale_files(reg);
    if stale.is_empty() && deleted.is_empty() {
        return Ok((existing, "disk"));
    }
    if stale.len() + deleted.len() > REBUILD_THRESHOLD {
        let idx = full_build(project_root, reg)?;
        idx.save()?;
        return Ok((idx, "rebuild"));
    }
    existing.refresh(&stale, &deleted, reg);

    let total = existing.symbols.len() as i64;
    if existing.dead_rows >= COMPACT_MIN
        && total > 0
        && existing.dead_rows as f64 / total as f64 >= COMPACT_RATIO
    {
        let idx = full_build(project_root, reg)?;
        idx.save()?;
        return Ok((idx, "compact"));
    }
    existing.save()?;
    Ok((existing, "refresh"))
}

// ---------------------------------------------------------- small helpers

fn mtime_f64(path: &Path) -> f64 {
    mtime_f64_checked(path).unwrap_or(0.0)
}

fn mtime_f64_checked(path: &Path) -> Option<f64> {
    let meta = std::fs::metadata(path).ok()?;
    let mt = meta.modified().ok()?;
    let d = mt.duration_since(std::time::UNIX_EPOCH).ok()?;
    Some(d.as_nanos() as f64 / 1e9)
}

fn git(cwd: &Path, args: &[&str]) -> Option<String> {
    git_bytes(cwd, args).map(|b| String::from_utf8_lossy(&b).into_owned())
}

fn git_bytes(cwd: &Path, args: &[&str]) -> Option<Vec<u8>> {
    let out = Command::new("git").args(args).current_dir(cwd).output().ok()?;
    out.status.success().then_some(out.stdout)
}
