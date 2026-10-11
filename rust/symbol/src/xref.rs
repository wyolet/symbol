//! Type-resolved cross-references: every use of one declaration, and the
//! implements relation of a type. The adapter does the resolution; this
//! layer picks the target from the index and maps result sites back onto
//! index symbols.

use anyhow::Result;

use crate::adapters::{error_json, Adapter, Registry, ResolvedSymbol, XrefTarget};
use crate::index::Index;

pub fn references(index: &Index, qualified_path: &str, reg: &Registry) -> serde_json::Value {
    let resolved = resolve(index, qualified_path, reg, |adapter, target| {
        adapter.references(target)
    });
    let (result, qualified_path) = match resolved {
        Ok(found) => found,
        Err(err) => return err,
    };
    let leaf = leaf_of(&qualified_path);
    let references: Vec<serde_json::Value> = result
        .references
        .iter()
        .map(|site| {
            let enclosing = innermost_row(index, &site.file, site.byte_start);
            serde_json::json!({
                "source_path": enclosing.map(|row| index.path_of(row)),
                "source_file": site.file,
                "source_range": enclosing.map(|row| {
                    let (s, e) = index.range_of(row);
                    vec![s, e]
                }),
                "ref_name": leaf,
                "ref_line": site.line,
                "ref_col": site.col,
            })
        })
        .collect();
    serde_json::json!({
        "status": "ok",
        "qualified_path": qualified_path,
        "target": target_json(&result.target),
        "count": references.len(),
        "references": references,
        "not_analyzed": result.not_analyzed,
    })
}

pub fn implementers(index: &Index, qualified_path: &str, reg: &Registry) -> serde_json::Value {
    let resolved = resolve(index, qualified_path, reg, |adapter, target| {
        adapter.implementers(target)
    });
    let (result, qualified_path) = match resolved {
        Ok(found) => found,
        Err(err) => return err,
    };
    let entries = |items: &[crate::adapters::Implementation]| -> Vec<serde_json::Value> {
        items
            .iter()
            .map(|item| {
                let mut entry = target_json(&item.symbol);
                entry["path"] = indexed_path(index, &item.symbol).into();
                entry["pointer"] = item.pointer.into();
                entry
            })
            .collect()
    };
    serde_json::json!({
        "status": "ok",
        "qualified_path": qualified_path,
        "target": target_json(&result.target),
        "implementers": entries(&result.implementers),
        "interfaces": entries(&result.interfaces),
        "not_analyzed": result.not_analyzed,
    })
}

/// Resolve `qualified_path` to one index row and ask its language's
/// adapter. A path the index lacks (a dependency's declaration, a struct
/// field) goes to every indexed language's adapter by name; the first that
/// resolves it answers. Returns the result and the path actually queried.
fn resolve<T>(
    index: &Index,
    qualified_path: &str,
    reg: &Registry,
    query: impl Fn(&Adapter, &XrefTarget) -> Result<T>,
) -> Result<(T, String), serde_json::Value> {
    let mut rows = matching_rows(index, qualified_path);
    // Rows sharing one path are build variants of a declaration (e.g. files
    // behind build constraints); keep those the adapter actually analyzes.
    if rows.len() > 1
        && rows
            .iter()
            .all(|&r| index.path_of(r) == index.path_of(rows[0]))
    {
        let mut answered = vec![];
        let mut last_err = None;
        for &row in &rows {
            match query_row(index, row, reg, &query) {
                Ok(result) => answered.push((row, result)),
                Err(err) if err["error_code"] == "not_analyzed" => last_err = Some(err),
                Err(err) => return Err(err),
            }
        }
        if answered.len() == 1 {
            let (row, result) = answered.remove(0);
            return Ok((result, index.path_of(row).to_owned()));
        }
        if answered.is_empty() {
            return Err(last_err.unwrap_or_default());
        }
        rows = answered.into_iter().map(|(row, _)| row).collect();
    }
    if rows.len() > 1 {
        let candidates: Vec<serde_json::Value> = rows
            .iter()
            .map(|&row| {
                let (s, e) = index.range_of(row);
                serde_json::json!({
                    "path": index.path_of(row),
                    "file": index.file_of(row),
                    "start_line": s,
                    "end_line": e,
                    "kind": index.kind_of(row),
                })
            })
            .collect();
        return Err(serde_json::json!({
            "status": "error", "error_code": "symbol_ambiguous",
            "message": format!("{} symbols match {qualified_path:?}", rows.len()),
            "candidates": candidates,
        }));
    }
    if let Some(&row) = rows.first() {
        return query_row(index, row, reg, &query)
            .map(|result| (result, index.path_of(row).to_owned()));
    }

    let mut found = vec![];
    for path in by_name_candidates(index, qualified_path) {
        if let Some(result) = query_by_name(index, &path, reg, &query)? {
            found.push((result, path));
        }
    }
    match found.len() {
        0 => Err(serde_json::json!({
            "status": "error", "error_code": "symbol_not_found",
            "message": format!("no symbol matches {qualified_path:?}\nTry: symbol search <name>"),
        })),
        1 => Ok(found.remove(0)),
        n => Err(serde_json::json!({
            "status": "error", "error_code": "symbol_ambiguous",
            "message": format!("{n} symbols match {qualified_path:?}"),
            "candidates": found.iter().map(|(_, path)| serde_json::json!({"path": path})).collect::<Vec<_>>(),
        })),
    }
}

/// Ask the row's language adapter about the declaration the row records.
fn query_row<T>(
    index: &Index,
    row: i64,
    reg: &Registry,
    query: &impl Fn(&Adapter, &XrefTarget) -> Result<T>,
) -> Result<T, serde_json::Value> {
    let path = index.path_of(row);
    let adapter = reg
        .for_language(index.language_of(row))
        .map_err(|err| error_json(&err, "no_adapter"))?;
    let target = XrefTarget {
        qualified_path: path,
        leaf: leaf_of(path),
        decl: Some((index.file_of(row), index.byte_range_of(row))),
    };
    query(adapter, &target).map_err(|err| error_json(&err, "adapter_failed"))
}

/// Ask each indexed language's adapter to resolve `path` by name. None when
/// none knows it; an error when it fails otherwise, or when the only
/// adapter does not support the query.
fn query_by_name<T>(
    index: &Index,
    path: &str,
    reg: &Registry,
    query: &impl Fn(&Adapter, &XrefTarget) -> Result<T>,
) -> Result<Option<T>, serde_json::Value> {
    let target = XrefTarget {
        qualified_path: path,
        leaf: leaf_of(path),
        decl: None,
    };
    let languages = indexed_languages(index);
    for language in &languages {
        let adapter = reg
            .for_language(language)
            .map_err(|err| error_json(&err, "no_adapter"))?;
        let err = match query(adapter, &target) {
            Ok(result) => return Ok(Some(result)),
            Err(err) => error_json(&err, "adapter_failed"),
        };
        match err["error_code"].as_str() {
            Some("unsupported") if languages.len() == 1 => return Err(err),
            Some("unsupported" | "symbol_not_found") => {}
            _ => return Err(err),
        }
    }
    Ok(None)
}

/// Exact qualified path first, else every path ending in it at a segment
/// boundary (`.` or `/`).
fn matching_rows(index: &Index, qualified_path: &str) -> Vec<i64> {
    if let Some(rows) = index.by_path.get(qualified_path) {
        return rows.clone();
    }
    let mut rows: Vec<i64> = index
        .by_path
        .iter()
        .filter(|(path, _)| {
            path.strip_suffix(qualified_path)
                .is_some_and(|head| head.ends_with('.') || head.ends_with('/'))
        })
        .flat_map(|(_, rows)| rows.iter().copied())
        .collect();
    rows.sort_unstable();
    rows
}

/// Full paths to try for a path the index lacks: a member it doesn't record
/// (e.g. a struct field) under every indexed container that matches, then
/// the path as given (a dependency's declaration).
fn by_name_candidates(index: &Index, qualified_path: &str) -> Vec<String> {
    let mut paths: Vec<String> = vec![];
    if let Some((container, member)) = qualified_path.rsplit_once('.') {
        for row in matching_rows(index, container) {
            let path = format!("{}.{member}", index.path_of(row));
            if !paths.contains(&path) {
                paths.push(path);
            }
        }
    }
    if !paths.iter().any(|p| p == qualified_path) {
        paths.push(qualified_path.to_owned());
    }
    paths
}

fn indexed_languages(index: &Index) -> Vec<String> {
    let mut languages: Vec<String> = index
        .files
        .iter()
        .filter_map(|rel| index.language_of_file(rel))
        .map(str::to_owned)
        .collect();
    languages.sort();
    languages.dedup();
    languages
}

fn leaf_of(qualified_path: &str) -> &str {
    qualified_path.rsplit('.').next().unwrap_or(qualified_path)
}

/// The smallest index symbol in `file` whose byte range contains `offset`.
fn innermost_row(index: &Index, file: &str, offset: i64) -> Option<i64> {
    let file_id = index.file_id_of(file)?;
    index
        .by_file
        .get(&file_id)?
        .iter()
        .copied()
        .filter(|&row| {
            let (s, e) = index.byte_range_of(row);
            s <= offset && offset < e
        })
        .min_by_key(|&row| {
            let (s, e) = index.byte_range_of(row);
            e - s
        })
}

/// The index address of a declaration site, so results can be fed to
/// `code` / `outline`; the adapter's own path when the index lacks it.
fn indexed_path(index: &Index, symbol: &ResolvedSymbol) -> String {
    if !symbol.external {
        if let Some(row) = innermost_row(index, &symbol.site.file, symbol.site.byte_start) {
            let path = index.path_of(row);
            if leaf_of(path) == leaf_of(&symbol.qualified_path) {
                return path.to_owned();
            }
        }
    }
    symbol.qualified_path.clone()
}

fn target_json(symbol: &ResolvedSymbol) -> serde_json::Value {
    serde_json::json!({
        "qualified_path": symbol.qualified_path,
        "kind": symbol.kind,
        "external": symbol.external,
        "file": symbol.site.file,
        "line": symbol.site.line,
        "col": symbol.site.col,
    })
}
