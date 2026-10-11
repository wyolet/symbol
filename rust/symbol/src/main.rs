use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

use symbol_core::adapters::{default_registry, DaemonUnavailable};
use symbol_core::index::{full_build, get_or_build, Index};
use symbol_core::reads;
use symbol_core::writes::ops::{delete_symbol, insert_symbol, replace_symbol};
use symbol_core::writes::patch::{
    apply_multi_patch, apply_patch, preflight, validate_args, MultiPatchEdit,
};
use symbol_core::writes::read_cache::ReadCache;
use symbol_core::writes::rename::rename_symbol;
use symbol_core::writes::tx::undo_last;

/// symbol — AST-native code intelligence (index, reads, writes, loc).
/// Language analysis runs in native-AST daemons (pyscan / go-scan).
#[derive(Parser)]
#[command(version, about)]
struct Args {
    /// Project root
    #[arg(long, default_value = ".", global = true)]
    root: PathBuf,

    /// JSON output
    #[arg(long, global = true)]
    json: bool,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Build the symbol index and write .symbol/symbol_index.msgpack.zst
    Index,
    /// Reindex changed files in place
    Refresh,
    /// Narrow candidates by name; signatures only
    Search {
        patterns: Vec<String>,
        #[arg(long)]
        kind: Option<String>,
        #[arg(long)]
        file: Option<String>,
        #[arg(long)]
        regex: bool,
        #[arg(long)]
        fixed: bool,
        #[arg(short = 'i', long)]
        ignore_case: bool,
        #[arg(long, default_value_t = 100)]
        limit: usize,
    },
    /// Retrieve exact body at a known address (file:A-B or qualified.path)
    Code { target: String },
    /// Parent-child symbol tree for a file or symbol
    Outline { query: String },
    /// Tier-1 textual reference scan
    Callers { name: String },
    /// Type-resolved references to one symbol (adapter-backed)
    Refs { qualified_path: String },
    /// Implementers of an interface, or interfaces a type implements
    Implementers { qualified_path: String },
    /// Byte-range edit primitive (replace / delete via empty content)
    Patch {
        file: String,
        range: String,
        content: Option<String>,
        #[arg(long)]
        force: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Apply multiple non-overlapping edits to one file atomically
    MultiPatch {
        file: String,
        /// JSON array of {range|old, content} edit objects
        edits: String,
        #[arg(long)]
        force: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Remove a named symbol (refuses if callers exist; --force overrides)
    DeleteSymbol {
        qualified_path: String,
        #[arg(long)]
        force: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Replace a symbol's full definition (renames callers if the name changes)
    ReplaceSymbol {
        qualified_path: String,
        content: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Insert content at a position anchored to a symbol (before/after/start/end)
    InsertSymbol {
        anchor_path: String,
        position: String,
        content: String,
        #[arg(long)]
        no_reindent: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Rename a symbol and its references (AST-aware with tier-1 fallback)
    RenameSymbol {
        qualified_path: String,
        new_name: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Revert the most recent rename/replace/delete transaction
    Undo,
    /// Index statistics
    Stats,
    /// Lines of code by language (GitHub-Linguist port)
    Loc {
        /// Project root to scan (defaults to --root)
        path: Option<PathBuf>,
        #[arg(long)]
        files: bool,
        #[arg(long)]
        classify: bool,
        #[arg(long)]
        exclude: Vec<String>,
        #[arg(long)]
        no_vendor_skip: bool,
        #[arg(long)]
        no_heuristics: bool,
        #[arg(long)]
        no_cache: bool,
        #[arg(long)]
        config_dir: Option<PathBuf>,
        #[arg(short, long)]
        verbose: bool,
    },
}

fn main() -> Result<()> {
    let args = Args::parse();
    let json = args.json;
    let Err(err) = run(args) else {
        return Ok(());
    };
    let Some(unavailable) = err.downcast_ref::<DaemonUnavailable>() else {
        return Err(err);
    };
    if json {
        println!("{}", serde_json::to_string_pretty(&unavailable.to_json())?);
    } else {
        eprintln!("error [daemon_unavailable]: {unavailable}");
    }
    std::process::exit(1);
}

fn run(args: Args) -> Result<()> {
    let root = args.root.canonicalize()?;
    let reg = default_registry(&root);

    match args.cmd {
        Cmd::Index => {
            let t0 = std::time::Instant::now();
            let idx = full_build(&root, reg)?;
            let build_ms = t0.elapsed().as_millis();
            let t1 = std::time::Instant::now();
            let path = idx.save()?;
            let save_ms = t1.elapsed().as_millis();
            let stats = idx.stats();
            if args.json {
                println!(
                    "{}",
                    serde_json::json!({"stats": stats, "path": path, "build_ms": build_ms, "save_ms": save_ms})
                );
            } else {
                println!(
                    "indexed {} files → {} symbols, {} imports, {} refs",
                    stats["files"], stats["symbols"], stats["imports"], stats["refs"]
                );
                println!(
                    "build {build_ms} ms  •  save {save_ms} ms  •  {}",
                    path.display()
                );
            }
        }
        Cmd::Refresh => {
            let (idx, source) = get_or_build(&root, reg)?;
            let stats = idx.stats();
            if args.json {
                println!("{}", serde_json::json!({"source": source, "stats": stats}));
            } else {
                println!(
                    "{source}: {} files, {} live symbols",
                    stats["files"], stats["live_symbols"]
                );
            }
        }
        Cmd::Search {
            patterns,
            kind,
            file,
            regex,
            fixed,
            ignore_case,
            limit,
        } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let hits = reads::search(
                &idx,
                &patterns,
                &reads::SearchOpts {
                    kind: kind.as_deref(),
                    file: file.as_deref(),
                    regex,
                    fixed,
                    ignore_case,
                    limit,
                },
                reg,
            )?;
            if args.json {
                println!("{}", serde_json::to_string_pretty(&hits)?);
            } else {
                println!("{} matches\n", hits.len());
                for h in &hits {
                    println!(
                        "{}:{}-{}",
                        h["file"].as_str().unwrap_or(""),
                        h["start_line"],
                        h["end_line"]
                    );
                    println!("{}", h["signature"].as_str().unwrap_or(""));
                    println!("{}", "─".repeat(78));
                }
                if !hits.is_empty() {
                    println!("Use symbol code <file:start-end> to fetch a body.");
                }
            }
        }
        Cmd::Code { target } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let payload = reads::code(&idx, &target, reg)?;
            record_read(&root, &idx, &payload);
            if args.json {
                println!("{}", serde_json::to_string_pretty(&payload)?);
            } else if payload["ambiguous"].as_bool() == Some(true) {
                println!("ambiguous — candidates:");
                for c in payload["candidates"].as_array().unwrap() {
                    println!(
                        "  {}  {}:{}-{}",
                        c["path"].as_str().unwrap_or(""),
                        c["file"].as_str().unwrap_or(""),
                        c["start_line"],
                        c["end_line"]
                    );
                }
            } else {
                println!(
                    "{}  {}:{}-{}  ({})",
                    payload["path"].as_str().unwrap_or("<slice>"),
                    payload["file"].as_str().unwrap_or(""),
                    payload["start_line"],
                    payload["end_line"],
                    payload["kind"].as_str().unwrap_or(""),
                );
                println!("{}", payload["body"].as_str().unwrap_or(""));
            }
        }
        Cmd::Outline { query } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let tree = reads::outline(&idx, &query, reg);
            if args.json {
                println!("{}", serde_json::to_string_pretty(&tree)?);
            } else {
                for node in &tree {
                    print_tree(node, 0);
                }
            }
        }
        Cmd::Callers { name } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let out = reads::callers(&idx, &name);
            if args.json {
                println!("{}", serde_json::to_string_pretty(&out)?);
            } else {
                println!("{} reference(s)", out.len());
                for c in &out {
                    println!(
                        "  {}:{}  {}  ({})",
                        c["source_file"].as_str().unwrap_or(""),
                        c["ref_line"],
                        c["source_path"].as_str().unwrap_or(""),
                        c["ref_kind"].as_str().unwrap_or(""),
                    );
                }
            }
        }
        Cmd::Refs { qualified_path } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let result = symbol_core::xref::references(&idx, &qualified_path, reg);
            finish_xref(&result, args.json, print_refs)?;
        }
        Cmd::Implementers { qualified_path } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let result = symbol_core::xref::implementers(&idx, &qualified_path, reg);
            finish_xref(&result, args.json, print_implementers)?;
        }
        Cmd::Patch {
            file,
            range,
            content,
            force,
            dry_run,
        } => {
            let cache = ReadCache::for_session(&root);
            let request = match validate_args(
                &file,
                &range,
                content.unwrap_or_default().as_bytes(),
                &root,
                force,
            ) {
                Ok(r) => r,
                Err(e) => {
                    println!("{}", serde_json::to_string_pretty(&e)?);
                    std::process::exit(1);
                }
            };
            if let Err(msg) = preflight(&request, &cache) {
                println!(
                    "{}",
                    serde_json::json!({"status": "needs_read_confirmation", "message": msg})
                );
                std::process::exit(1);
            }
            let result = apply_patch(&request, &cache, dry_run, 5);
            let failed = result.status == "error";
            if args.json {
                println!("{}", serde_json::to_string_pretty(&result)?);
            } else if failed {
                println!(
                    "error [{}]: {}",
                    result.error_code.as_deref().unwrap_or(""),
                    result.message.as_deref().unwrap_or("")
                );
            } else {
                println!(
                    "{} {}  -{} +{}",
                    result.status, result.file_rel, result.lines_removed, result.lines_added
                );
                println!("{}", result.diff);
            }
            if failed {
                std::process::exit(1);
            }
        }
        Cmd::MultiPatch {
            file,
            edits,
            force,
            dry_run,
        } => {
            let parsed: Vec<MultiPatchEdit> = match serde_json::from_str(&edits) {
                Ok(edits) => edits,
                Err(error) => {
                    println!(
                        "{}",
                        serde_json::json!({
                            "status": "error",
                            "file_rel": file,
                            "error_code": "invalid_argument",
                            "message": format!("edits must be a JSON array: {error}")
                        })
                    );
                    std::process::exit(1);
                }
            };
            let cache = ReadCache::for_session(&root);
            let result = apply_multi_patch(&file, &parsed, &root, &cache, dry_run, force, 5);
            let failed = matches!(result.status, "error" | "needs_read_confirmation");
            println!("{}", serde_json::to_string_pretty(&result)?);
            if failed {
                std::process::exit(1);
            }
        }
        Cmd::DeleteSymbol {
            qualified_path,
            force,
            dry_run,
        } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = delete_symbol(&mut idx, &qualified_path, &root, reg, force, dry_run);
            finish_write(&root, &mut idx, reg, result, dry_run)?;
        }
        Cmd::ReplaceSymbol {
            qualified_path,
            content,
            dry_run,
        } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = replace_symbol(&mut idx, &qualified_path, &content, &root, reg, dry_run);
            finish_write(&root, &mut idx, reg, result, dry_run)?;
        }
        Cmd::InsertSymbol {
            anchor_path,
            position,
            content,
            no_reindent,
            dry_run,
        } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = insert_symbol(
                &mut idx,
                &anchor_path,
                &position,
                &content,
                !no_reindent,
                &root,
                reg,
                dry_run,
            );
            finish_write(&root, &mut idx, reg, result, dry_run)?;
        }
        Cmd::RenameSymbol {
            qualified_path,
            new_name,
            dry_run,
        } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = rename_symbol(&mut idx, &qualified_path, &new_name, &root, reg, dry_run);
            finish_write(&root, &mut idx, reg, result, dry_run)?;
        }
        Cmd::Undo => {
            let result = undo_last(&root);
            if result.status == "undone" && !result.files_restored.is_empty() {
                // Do not rely on mtime detection here: a fast restore can land
                // within the filesystem's timestamp granularity. Mirror the
                // Python command and explicitly refresh every restored file.
                if let Some(mut idx) = Index::load(&root) {
                    let stale = result.files_restored.iter().cloned().collect();
                    match idx.refresh(&stale, &Default::default(), reg) {
                        Ok(()) => {
                            idx.save()?;
                        }
                        Err(err) => eprintln!("warning: index not refreshed after undo: {err:#}"),
                    }
                }
            }
            println!("{}", serde_json::to_string_pretty(&result)?);
            if result.status == "error" {
                std::process::exit(1);
            }
        }
        Cmd::Loc {
            path,
            files,
            classify,
            exclude,
            no_vendor_skip,
            no_heuristics,
            no_cache,
            config_dir,
            verbose,
        } => {
            return symbol_linguist::loc::run(symbol_linguist::loc::LocArgs {
                path: path.unwrap_or(root),
                json: args.json,
                files,
                classify,
                exclude,
                no_vendor_skip,
                no_heuristics,
                no_cache,
                config_dir,
                verbose,
            });
        }
        Cmd::Stats => {
            let (idx, source) = get_or_build(&root, reg)?;
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"source": source, "stats": idx.stats()})
                )?
            );
        }
    }
    Ok(())
}

/// After a successful write: refresh the touched files in the index and
/// persist it, so the next command sees fresh byte ranges.
fn finish_write(
    _root: &std::path::Path,
    idx: &mut Index,
    reg: &'static symbol_core::adapters::Registry,
    result: serde_json::Value,
    dry_run: bool,
) -> Result<()> {
    let failed = result["status"] == "error";
    if !failed && !dry_run {
        let mut touched: std::collections::HashSet<String> = Default::default();
        for key in ["file_rel", "declaring_file"] {
            if let Some(f) = result[key].as_str() {
                touched.insert(f.to_owned());
            }
        }
        if let Some(per_file) = result["per_file"].as_array() {
            for f in per_file {
                if let Some(rel) = f["file"].as_str() {
                    touched.insert(rel.to_owned());
                }
            }
        }
        if !touched.is_empty() {
            match idx.refresh(&touched, &Default::default(), reg) {
                Ok(()) => {
                    let _ = idx.save();
                }
                Err(err) => eprintln!("warning: index not refreshed after write: {err:#}"),
            }
        }
    }
    println!("{}", serde_json::to_string_pretty(&result)?);
    if failed {
        std::process::exit(1);
    }
    Ok(())
}

/// Record served bodies in the session read cache so a follow-up patch
/// passes preflight — mirrors how commands/code.py records reads.
fn record_read(root: &std::path::Path, _idx: &Index, payload: &serde_json::Value) {
    use symbol_core::writes::patch::{
        line_end_byte, line_start_byte, mtime_f64, now_f64, short_sha256,
    };
    use symbol_core::writes::read_cache::CachedRead;

    let Some(file) = payload["file"].as_str() else {
        return;
    };
    let Some(start_line) = payload["start_line"].as_i64() else {
        return;
    };
    let Some(end_line) = payload["end_line"].as_i64() else {
        return;
    };
    let Ok(source) = std::fs::read(root.join(file)) else {
        return;
    };
    let start = line_start_byte(&source, start_line);
    let end = line_end_byte(&source, end_line);
    let cache = ReadCache::for_session(root);
    if let Some(mt) = mtime_f64(&root.join(file)) {
        cache.record(&CachedRead {
            file: file.to_owned(),
            byte_range: (start, end),
            content_hash: short_sha256(&source[start..end]),
            served_at: now_f64(),
            served_mtime: mt,
            tool_call_idx: 0,
        });
    }
}

/// Print a refs/implementers result; errors print as `error [code]` (or
/// the JSON object) and exit 1.
fn finish_xref(
    result: &serde_json::Value,
    json: bool,
    print: fn(&serde_json::Value),
) -> Result<()> {
    let failed = result["status"] == "error";
    if json {
        println!("{}", serde_json::to_string_pretty(result)?);
    } else if failed {
        eprintln!(
            "error [{}]: {}",
            result["error_code"].as_str().unwrap_or(""),
            result["message"].as_str().unwrap_or("")
        );
        for c in result["candidates"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[])
        {
            eprintln!(
                "  {}  {}:{}-{}",
                c["path"].as_str().unwrap_or(""),
                c["file"].as_str().unwrap_or(""),
                c["start_line"],
                c["end_line"]
            );
        }
        for e in result["errors"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[])
        {
            eprintln!("  {}", e.as_str().unwrap_or(""));
        }
    } else {
        print(result);
    }
    if failed {
        std::process::exit(1);
    }
    Ok(())
}

fn xref_header(result: &serde_json::Value) -> String {
    let target = &result["target"];
    format!(
        "{} ({}, {}:{})",
        result["qualified_path"].as_str().unwrap_or(""),
        target["kind"].as_str().unwrap_or(""),
        target["file"].as_str().unwrap_or(""),
        target["line"],
    )
}

fn print_not_analyzed(result: &serde_json::Value) {
    let skipped = result["not_analyzed"].as_array().map_or(0, Vec::len);
    if skipped > 0 {
        println!(
            "note: {skipped} source file(s) were not analyzed (e.g. excluded by build constraints); --json lists them"
        );
    }
}

fn print_refs(result: &serde_json::Value) {
    println!("{}: {} reference(s)", xref_header(result), result["count"]);
    for r in result["references"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[])
    {
        println!(
            "  {}:{}:{}  {}",
            r["source_file"].as_str().unwrap_or(""),
            r["ref_line"],
            r["ref_col"],
            r["source_path"].as_str().unwrap_or("<top level>"),
        );
    }
    print_not_analyzed(result);
}

fn print_implementers(result: &serde_json::Value) {
    let (label, items) = if result["target"]["kind"] == "interface" {
        ("implementer(s)", &result["implementers"])
    } else {
        ("interface(s) implemented", &result["interfaces"])
    };
    let items = items.as_array().map(Vec::as_slice).unwrap_or(&[]);
    println!("{}: {} {label}", xref_header(result), items.len());
    for item in items {
        let mut notes = vec![];
        if item["pointer"] == true {
            notes.push("pointer receiver");
        }
        if item["external"] == true {
            notes.push("dependency");
        }
        let location = if item["external"] == true {
            String::new()
        } else {
            format!("{}:{}  ", item["file"].as_str().unwrap_or(""), item["line"])
        };
        let notes = if notes.is_empty() {
            String::new()
        } else {
            format!("  ({})", notes.join(", "))
        };
        println!("  {location}{}{notes}", item["path"].as_str().unwrap_or(""));
    }
    print_not_analyzed(result);
}

fn print_tree(node: &serde_json::Value, depth: usize) {
    let indent = "  ".repeat(depth);
    println!(
        "{indent}{} [{}] {}-{}  {}",
        node["path"].as_str().unwrap_or(""),
        node["kind"].as_str().unwrap_or(""),
        node["start_line"],
        node["end_line"],
        node["signature"].as_str().unwrap_or(""),
    );
    for child in node["children"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[])
    {
        print_tree(child, depth + 1);
    }
}
