//! Port of shared/linguist/language_stats.py. Field-name gotcha preserved:
//! the per-file record's `lines` is TOTAL lines (loc), while the aggregate's
//! JSON `lines` is sloc (backwards-compat alias in Python).

use serde::Serialize;

use crate::language::{LangId, Registry};

/// FileInfo — per-file stats collected during detection.
#[derive(Clone, Serialize)]
pub struct FileInfo {
    pub path: String,
    pub lines: u64, // total lines (loc)
    pub sloc: u64,  // non-blank
    pub size: u64,  // bytes (stat)
}

pub struct LangStats {
    pub name: String,
    pub id: i64,
    pub lang_type: String,
    pub color: Option<String>,
    pub popular: bool,
    pub is_counted: bool,
    pub loc: u64,
    pub sloc: u64,
    pub size: u64,
    pub files: u64,
    pub percentage_lines: f64,
    pub percentage_bytes: f64,
    pub file_stats: Vec<FileInfo>,
}

/// LanguageStatistics.dict() — exact key set and order.
#[derive(Serialize)]
pub struct LangStatsDict<'a> {
    pub loc: u64,
    pub sloc: u64,
    pub lines: u64, // == sloc, backwards compat
    pub size: u64,
    pub files: u64,
    pub percentage_lines: f64,
    pub percentage_bytes: f64,
    pub name: &'a str,
    pub id: i64,
    pub popular: bool,
    pub is_counted: bool,
    #[serde(rename = "type")]
    pub lang_type: &'a str,
    pub color: Option<&'a str>,
}

impl LangStats {
    fn new(reg: &Registry, id: LangId) -> LangStats {
        let lang = reg.get(id);
        LangStats {
            name: lang.name.clone(),
            id: lang.language_id,
            lang_type: lang.lang_type.clone(),
            color: lang.color.clone(),
            popular: lang.popular,
            is_counted: lang.lang_type == "programming" || lang.popular,
            loc: 0,
            sloc: 0,
            size: 0,
            files: 0,
            percentage_lines: 0.0,
            percentage_bytes: 0.0,
            file_stats: Vec::new(),
        }
    }

    pub fn dict(&self) -> LangStatsDict<'_> {
        LangStatsDict {
            loc: self.loc,
            sloc: self.sloc,
            lines: self.sloc,
            size: self.size,
            files: self.files,
            percentage_lines: self.percentage_lines,
            percentage_bytes: self.percentage_bytes,
            name: &self.name,
            id: self.id,
            popular: self.popular,
            is_counted: self.is_counted,
            lang_type: &self.lang_type,
            color: self.color.as_deref(),
        }
    }
}

/// The accumulator Linguist keeps in `self.statistics` — keyed by language,
/// insertion order = first-seen order (Python dict semantics).
#[derive(Default)]
pub struct Statistics {
    order: Vec<LangId>,
    by_lang: std::collections::HashMap<LangId, LangStats>,
}

impl Statistics {
    pub fn add(&mut self, reg: &Registry, id: LangId, path: String, loc: u64, sloc: u64, size: u64) {
        let stats = self.by_lang.entry(id).or_insert_with(|| {
            self.order.push(id);
            LangStats::new(reg, id)
        });
        stats.loc += loc;
        stats.sloc += sloc;
        stats.size += size;
        stats.files += 1;
        stats.file_stats.push(FileInfo {
            path,
            lines: loc,
            sloc,
            size,
        });
    }

    /// linguist.py calculate_percentages — over is_counted languages only.
    pub fn calculate_percentages(&mut self) {
        let (total_sloc, total_size) = self
            .by_lang
            .values()
            .filter(|s| s.is_counted)
            .fold((0u64, 0u64), |(l, s), st| (l + st.sloc, s + st.size));
        for st in self.by_lang.values_mut() {
            if !st.is_counted {
                continue;
            }
            st.percentage_lines = if total_sloc > 0 {
                st.sloc as f64 / total_sloc as f64 * 100.0
            } else {
                0.0
            };
            st.percentage_bytes = if total_size > 0 {
                st.size as f64 / total_size as f64 * 100.0
            } else {
                0.0
            };
        }
    }

    /// First-seen order, like iterating Python's statistics dict.
    pub fn iter(&self) -> impl Iterator<Item = &LangStats> {
        self.order.iter().map(|id| &self.by_lang[id])
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }
}
