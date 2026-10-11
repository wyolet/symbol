//! Port of writes/rename/index_query.py (RenamerIndexQuery): the index
//! questions a daemon asks back mid-rename (`index/*` requests), answered
//! from the in-memory index.

use std::path::Path;

use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};

use crate::index::{Index, S_FILE};

/// Dispatch one `index/*` request from a daemon.
pub fn serve(index: &Index, project_root: &Path, method: &str, params: &Value) -> Result<Value> {
    let arg = |key: &str| {
        params[key]
            .as_str()
            .ok_or_else(|| anyhow!("{method}: missing string param {key:?}"))
    };
    match method {
        "index/find_declaration" => Ok(find_declaration(index, project_root, arg("qpath")?)),
        "index/class_bases" => Ok(json!(class_bases(index, arg("qpath")?))),
        "index/owners_of_leaf" => Ok(json!(owners_of_leaf(index, arg("leaf")?))),
        other => bail!("unknown index query {other:?}"),
    }
}

fn find_declaration(index: &Index, project_root: &Path, qpath: &str) -> Value {
    let Some(&row) = index.by_path.get(qpath).and_then(|rows| rows.first()) else {
        return Value::Null;
    };
    let rel = index.file_of(row);
    let (s, e) = index.byte_range_of(row);
    json!({
        "path": project_root.join(rel).to_string_lossy(),
        "file": rel,
        "byte_range": [s, e],
        "kind": index.kind_of(row),
    })
}

/// Base expressions resolved same-file first, then through imports;
/// unresolvable bases are dropped. Source base order is kept.
fn class_bases(index: &Index, class_qpath: &str) -> Vec<String> {
    let Some(&row) = index.by_path.get(class_qpath).and_then(|rows| rows.first()) else {
        return vec![];
    };
    let file_id = index.symbols[row as usize][S_FILE];
    let mut ancestors: Vec<&str> = class_qpath.split('.').collect();
    ancestors.pop();
    let imports = index.imports_for(file_id);
    index
        .bases_for(row)
        .into_iter()
        .filter_map(|base| {
            resolve_same_file(index, base, &ancestors, file_id)
                .or_else(|| resolve_import(base, &imports))
        })
        .collect()
}

fn resolve_same_file(
    index: &Index,
    base: &str,
    ancestors: &[&str],
    file_id: i64,
) -> Option<String> {
    for i in (0..=ancestors.len()).rev() {
        let candidate = ancestors[..i]
            .iter()
            .copied()
            .chain([base])
            .collect::<Vec<_>>()
            .join(".");
        if let Some(rows) = index.by_path.get(&candidate) {
            if rows
                .iter()
                .any(|&r| index.symbols[r as usize][S_FILE] == file_id)
            {
                return Some(candidate);
            }
        }
    }
    None
}

fn resolve_import(base: &str, imports: &[(&str, &str, i64)]) -> Option<String> {
    let head = base.split('.').next().unwrap_or(base);
    let tail = &base[head.len()..];
    let (_, source, _) = imports.iter().find(|(local, _, _)| *local == head)?;
    if base == *source || base.starts_with(&format!("{source}.")) || *source == head {
        return Some(base.to_owned());
    }
    if !tail.is_empty() {
        return Some(format!("{source}{tail}"));
    }
    Some(format!("{source}.{base}"))
}

fn owners_of_leaf(index: &Index, leaf: &str) -> Vec<String> {
    let suffix = format!(".{leaf}");
    index
        .by_path
        .keys()
        .filter(|qp| *qp == leaf || qp.ends_with(&suffix))
        .cloned()
        .collect()
}
