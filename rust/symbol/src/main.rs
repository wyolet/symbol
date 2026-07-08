use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

use symbol_core::adapters::default_registry;
use symbol_core::index::{full_build, get_or_build, Index};
use symbol_core::reads;
use symbol_core::writes::ops::{delete_symbol, insert_symbol, replace_symbol};
use symbol_core::writes::patch::{apply_patch, preflight, validate_args};
use symbol_core::writes::read_cache::ReadCache;
use symbol_core::writes::rename::{rename_parameter, rename_symbol};
use symbol_core::writes::tx::undo_last;

/// symbol-rs — Rust port of symbol's core (index, reads, writes).
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
    /// Rename a symbol and its references (tier-1 textual)
    RenameSymbol {
        qualified_path: String,
        new_name: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Rename a function/method parameter (signature slot, body uses, resolvable kwargs)
    RenameParameter {
        func_qpath: String,
        param: String,
        new_name: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Revert the most recent rename/replace/delete transaction
    Undo,
    /// Index statistics
    Stats,
}

fn main() -> Result<()> {
    let args = Args::parse();
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
                println!("{}", serde_json::json!({"stats": stats, "path": path, "build_ms": build_ms, "save_ms": save_ms}));
            } else {
                println!(
                    "indexed {} files → {} symbols, {} imports, {} refs",
                    stats["files"], stats["symbols"], stats["imports"], stats["refs"]
                );
                println!("build {build_ms} ms  •  save {save_ms} ms  •  {}", path.display());
            }
        }
        Cmd::Refresh => {
            let (idx, source) = get_or_build(&root, reg)?;
            let stats = idx.stats();
            if args.json {
                println!("{}", serde_json::json!({"source": source, "stats": stats}));
            } else {
                println!("{source}: {} files, {} live symbols", stats["files"], stats["live_symbols"]);
            }
        }
        Cmd::Search { patterns, kind, file, regex, fixed, ignore_case, limit } => {
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
                    println!("Use symbol-rs code <file:start-end> to fetch a body.");
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
        Cmd::Patch { file, range, content, force, dry_run } => {
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
                println!("error [{}]: {}", result.error_code.as_deref().unwrap_or(""), result.message.as_deref().unwrap_or(""));
            } else {
                println!("{} {}  -{} +{}", result.status, result.file_rel, result.lines_removed, result.lines_added);
                println!("{}", result.diff);
            }
            if failed {
                std::process::exit(1);
            }
        }
        Cmd::DeleteSymbol { qualified_path, force, dry_run } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = delete_symbol(&mut idx, &qualified_path, &root, reg, force, dry_run);
            finish_write(&root, &mut idx, reg, result, args.json, dry_run)?;
        }
        Cmd::ReplaceSymbol { qualified_path, content, dry_run } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = replace_symbol(&mut idx, &qualified_path, &content, &root, reg, dry_run);
            finish_write(&root, &mut idx, reg, result, args.json, dry_run)?;
        }
        Cmd::InsertSymbol { anchor_path, position, content, no_reindent, dry_run } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = insert_symbol(
                &mut idx, &anchor_path, &position, &content, !no_reindent, &root, reg, dry_run,
            );
            finish_write(&root, &mut idx, reg, result, args.json, dry_run)?;
        }
        Cmd::RenameSymbol { qualified_path, new_name, dry_run } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = rename_symbol(&mut idx, &qualified_path, &new_name, &root, reg, dry_run);
            finish_write(&root, &mut idx, reg, result, args.json, dry_run)?;
        }
        Cmd::RenameParameter { func_qpath, param, new_name, dry_run } => {
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = rename_parameter(&mut idx, &func_qpath, &param, &new_name, &root, reg, dry_run);
            finish_write(&root, &mut idx, reg, result, args.json, dry_run)?;
        }
        Cmd::Undo => {
            let result = undo_last(&root);
            // Bring the index back in line with the restored tree.
            let _ = get_or_build(&root, reg);
            println!("{}", serde_json::to_string_pretty(&result)?);
            if result.status == "error" {
                std::process::exit(1);
            }
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
    json: bool,
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
            idx.refresh(&touched, &Default::default(), reg);
            let _ = idx.save();
        }
    }
    if json {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!("{}", serde_json::to_string_pretty(&result)?);
    }
    if failed {
        std::process::exit(1);
    }
    Ok(())
}

/// Record served bodies in the session read cache so a follow-up patch
/// passes preflight — mirrors how commands/code.py records reads.
fn record_read(root: &std::path::Path, idx: &Index, payload: &serde_json::Value) {
    use symbol_core::writes::patch::{mtime_f64, now_f64, short_sha256};
    use symbol_core::writes::read_cache::CachedRead;

    let Some(file) = payload["file"].as_str() else { return };
    let cache = ReadCache::for_session(root);
    let body = payload["body"].as_str().unwrap_or("");
    // Approximate byte range from the indexed row when present.
    let (Some(path), Some(_)) = (payload["path"].as_str(), payload["body"].as_str()) else {
        return;
    };
    if let Some(rows) = idx.by_path.get(path) {
        if rows.len() == 1 {
            let (s, e) = idx.byte_range_of(rows[0]);
            if let Some(mt) = mtime_f64(&root.join(file)) {
                cache.record(&CachedRead {
                    file: file.to_owned(),
                    byte_range: (s as usize, e as usize),
                    content_hash: short_sha256(body.as_bytes()),
                    served_at: now_f64(),
                    served_mtime: mt,
                    tool_call_idx: 0,
                });
            }
        }
    }
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
    for child in node["children"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
        print_tree(child, depth + 1);
    }
}
