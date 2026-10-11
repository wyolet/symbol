use std::path::{Path, PathBuf};

use anyhow::Result;
use clap::{Args as ClapArgs, CommandFactory, FromArgMatches, Parser, Subcommand};
use serde_json::Value;

use symbol_core::adapters::{default_registry, DaemonUnavailable};
use symbol_core::index::{full_build, get_or_build, Index};
use symbol_core::reads::{self, CodeMiss};
use symbol_core::render;
use symbol_core::writes::ops::{
    delete_symbol_with_cache, insert_symbol_with_cache, replace_symbol,
};
use symbol_core::writes::patch::{
    apply_multi_patch, apply_patch, preflight, validate_args, MultiPatchEdit,
};
use symbol_core::writes::read_cache::ReadCache;
use symbol_core::writes::rename::rename_symbol;
use symbol_core::writes::tx::undo_last;

#[cfg(test)]
#[path = "cli_surface_tests.rs"]
mod cli_surface_tests;

/// symbol — AST-native code intelligence for Python and Go.
///
/// Accepts the Python CLI's spelling (`--path`, `--format json`, options
/// for range/content/position) and its own (`--root`, `--json`, positional
/// range/content/position). `--json` keeps this binary's own JSON shapes;
/// `--format json` prints exactly what the Python CLI prints.
#[derive(Parser)]
#[command(name = "symbol", about, disable_version_flag = true)]
struct Cli {
    /// Project root
    #[arg(
        long = "root",
        visible_alias = "path",
        short = 'p',
        default_value = ".",
        global = true
    )]
    root: PathBuf,

    /// JSON output in this binary's own shapes
    #[arg(long, global = true)]
    json: bool,

    /// Output format for the subcommand: rich or json
    #[arg(long = "format", value_name = "FORMAT")]
    format: Option<String>,

    /// Show full detail
    #[arg(short = 'v', long = "verbose")]
    verbose: bool,

    /// Show the installed symbol version and exit
    #[arg(long)]
    version: bool,

    #[command(subcommand)]
    cmd: Option<Cmd>,
}

/// `--format/-f` (rich | json).
#[derive(ClapArgs, Default)]
struct Format {
    /// Output format: rich or json
    #[arg(long, short = 'f')]
    format: Option<String>,
}

/// `--format/-f` plus `--agent` (or SYMBOL_AGENT) plain-text mode.
#[derive(ClapArgs, Default)]
struct WriteFormat {
    /// Output format: rich, agent, or json
    #[arg(long, short = 'f')]
    format: Option<String>,
    /// Enriched plain-text output for LLM consumers
    #[arg(long)]
    agent: bool,
}

#[derive(Subcommand)]
enum Cmd {
    /// Build the symbol lookup table and write to .symbol/symbol_index.msgpack.zst
    Index {
        /// Project root (defaults to --root)
        path: Option<PathBuf>,
    },
    /// Reindex changed files and clear transaction history
    Refresh {
        /// Force full rebuild instead of incremental refresh
        #[arg(long)]
        full: bool,
        /// Don't clear .symbol/transactions/
        #[arg(long)]
        keep_transactions: bool,
        #[command(flatten)]
        out: WriteFormat,
    },
    /// Run the MCP server (stdio) exposing symbol as agent tools
    Mcp {
        /// Project root to serve (same as --root)
        #[arg(short = 'r', id = "mcp_root", value_name = "ROOT", default_value = ".")]
        root: PathBuf,
    },
    /// Narrow candidates by name. Returns signatures, no bodies.
    Search {
        /// One or more symbol patterns. Multiple patterns = AND (all must match).
        #[arg(required = true)]
        patterns: Vec<String>,
        /// Filter by kind: class, function, async_function
        #[arg(long)]
        kind: Option<String>,
        /// Restrict to one file (repo-relative path)
        #[arg(long)]
        file: Option<String>,
        /// Treat patterns as regex (unanchored)
        #[arg(long, short = 'E')]
        regex: bool,
        /// Treat patterns as literal substrings
        #[arg(long, short = 'F')]
        fixed: bool,
        /// Case-insensitive matching
        #[arg(long, short = 'i')]
        ignore_case: bool,
        /// Max candidates to return
        #[arg(long, default_value_t = 100)]
        limit: usize,
        #[command(flatten)]
        out: Format,
    },
    /// Retrieve exact body at a known address. Use after symbol search.
    Code {
        /// file:start-end or fully qualified symbol path
        target: String,
        /// Skip reference list in output
        #[arg(long)]
        no_refs: bool,
        #[command(flatten)]
        out: Format,
    },
    /// Show a file's symbols as a parent-child tree
    Outline {
        /// File (or qualified symbol path) to outline
        file: String,
        #[command(flatten)]
        out: Format,
    },
    /// Tier-1 textual reference scan — name-match only, unresolved
    Callers {
        /// Name to search for (last segment matched)
        name: String,
        #[command(flatten)]
        out: Format,
    },
    /// Type-resolved references to one symbol (adapter-backed)
    Refs {
        qualified_path: String,
        /// List every file that was not analyzed
        #[arg(short, long)]
        verbose: bool,
    },
    /// Implementers of an interface, or interfaces a type implements
    Implementers {
        qualified_path: String,
        /// List every file that was not analyzed
        #[arg(short, long)]
        verbose: bool,
    },
    /// Byte-range edit primitive. Replace, delete (empty content), or insert.
    Patch {
        /// File to patch
        file: String,
        /// Line range A-B (same as --range)
        #[arg(value_name = "RANGE")]
        range_arg: Option<String>,
        /// New content (same as --content)
        #[arg(value_name = "CONTENT")]
        content_arg: Option<String>,
        /// Line range A-B (inclusive, 1-indexed)
        #[arg(long = "range", short = 'r')]
        range: Option<String>,
        /// New content (use '' or omit for delete)
        #[arg(long = "content")]
        content: Option<String>,
        /// Skip read-cache check
        #[arg(long)]
        force: bool,
        /// Compute diff but don't write
        #[arg(long)]
        dry_run: bool,
        /// Lines of diff context around the edit
        #[arg(long, short = 'C', default_value_t = 5)]
        context: usize,
        #[command(flatten)]
        out: WriteFormat,
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
    /// Remove a named symbol from its file. Refuses if callers exist (use --force).
    DeleteSymbol {
        /// Fully qualified symbol path (e.g. services.user.UserService)
        qualified_path: String,
        /// Delete even if callers exist
        #[arg(long)]
        force: bool,
        /// Compute diff but don't write
        #[arg(long)]
        dry_run: bool,
        /// Lines of diff context
        #[arg(long, short = 'C', default_value_t = 5)]
        context: usize,
        #[command(flatten)]
        out: WriteFormat,
    },
    /// Replace a symbol's full definition. If content declares a new name, callers are updated too.
    ReplaceSymbol {
        /// Fully qualified symbol path to replace
        qualified_path: String,
        /// Full new symbol definition (same as --content)
        #[arg(value_name = "CONTENT")]
        content_arg: Option<String>,
        /// Full new symbol definition
        #[arg(long = "content")]
        content: Option<String>,
        /// Preview without writing
        #[arg(long)]
        dry_run: bool,
        #[command(flatten)]
        out: WriteFormat,
    },
    /// Insert code at a position anchored to a symbol (before/after/start/end).
    InsertSymbol {
        /// Anchor symbol qualified path
        anchor: String,
        /// before | after | start | end (same as --position)
        #[arg(value_name = "POSITION")]
        position_arg: Option<String>,
        /// New content (same as --content)
        #[arg(value_name = "CONTENT")]
        content_arg: Option<String>,
        /// before | after | start | end
        #[arg(long = "position")]
        position: Option<String>,
        /// New content
        #[arg(long = "content")]
        content: Option<String>,
        /// Send content as-is, don't auto-indent
        #[arg(long)]
        no_reindent: bool,
        /// Compute diff but don't write
        #[arg(long)]
        dry_run: bool,
        /// Lines of diff context
        #[arg(long, short = 'C', default_value_t = 5)]
        context: usize,
        #[command(flatten)]
        out: WriteFormat,
    },
    /// Rename a symbol and update references across the project.
    RenameSymbol {
        /// Fully qualified symbol path (e.g. services.user.UserService)
        qualified_path: String,
        /// New leaf name (no dots)
        new_name: String,
        /// Plan rename, don't write
        #[arg(long)]
        dry_run: bool,
        #[command(flatten)]
        out: WriteFormat,
    },
    /// Revert the most recent rename-symbol / replace-symbol transaction.
    Undo {
        #[command(flatten)]
        out: WriteFormat,
    },
    /// Index statistics
    Stats,
    /// Count lines of code by language — powered by GitHub Linguist.
    Loc {
        /// Project directory (defaults to --root)
        path: Option<PathBuf>,
        #[command(flatten)]
        out: Format,
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
    /// Module import graph: cycles, hotspots, fan-out, deep chains
    Map {
        /// Modules that import MODULE, directly or transitively
        #[arg(long, value_name = "MODULE", conflicts_with = "deps")]
        dependents: Option<String>,
        /// Modules MODULE imports, directly or transitively
        #[arg(long, value_name = "MODULE")]
        deps: Option<String>,
        /// Maximum import hops for --dependents / --deps
        #[arg(long)]
        depth: Option<usize>,
        /// Items per section (0 = all)
        #[arg(short = 'n', long, default_value_t = 10)]
        limit: usize,
        /// Leave out files matching this glob (repeatable), e.g. '*_test.go'
        #[arg(long)]
        exclude: Vec<String>,
        #[arg(short, long)]
        verbose: bool,
    },
    /// Hook entry — reads Claude Code hook JSON from stdin.
    #[command(hide = true)]
    Hook {
        /// Block tool calls (exit 2) instead of soft-nudging via additionalContext.
        #[arg(long)]
        enforce: bool,
    },
}

impl Cmd {
    /// The subcommand's own `--format`, when it has one.
    fn format_slot(&mut self) -> Option<&mut Option<String>> {
        match self {
            Cmd::Search { out, .. }
            | Cmd::Code { out, .. }
            | Cmd::Outline { out, .. }
            | Cmd::Callers { out, .. }
            | Cmd::Loc { out, .. } => Some(&mut out.format),
            Cmd::Refresh { out, .. }
            | Cmd::Patch { out, .. }
            | Cmd::DeleteSymbol { out, .. }
            | Cmd::ReplaceSymbol { out, .. }
            | Cmd::InsertSymbol { out, .. }
            | Cmd::RenameSymbol { out, .. }
            | Cmd::Undo { out } => Some(&mut out.format),
            _ => None,
        }
    }

    /// Commands the Python CLI does not have read the root `--format`
    /// as their JSON switch.
    fn native_only(&self) -> bool {
        matches!(
            self,
            Cmd::Refs { .. }
                | Cmd::Implementers { .. }
                | Cmd::MultiPatch { .. }
                | Cmd::Stats
                | Cmd::Map { .. }
        )
    }
}

/// How a command prints: its own JSON (`--json`), the Python CLI's JSON
/// (`--format json`), or text — Rich-style, or `--agent` plain.
#[derive(Clone, Copy)]
struct Out {
    native_json: bool,
    py_json: bool,
    agent: bool,
}

impl Out {
    fn read(native_json: bool, f: &Format) -> Out {
        Out {
            native_json,
            py_json: !native_json && f.format.as_deref() == Some("json"),
            agent: false,
        }
    }

    fn write(native_json: bool, f: &WriteFormat) -> Out {
        let env_agent = std::env::var("SYMBOL_AGENT").is_ok_and(|v| !v.is_empty());
        Out {
            native_json,
            py_json: !native_json && f.format.as_deref() == Some("json"),
            agent: f.agent || env_agent,
        }
    }
}

fn main() {
    let matches = Cli::command().get_matches();
    let mut cli = Cli::from_arg_matches(&matches).unwrap_or_else(|e| e.exit());
    let name = matches.subcommand_name().unwrap_or_default().to_owned();
    if cli.version {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let Some(cmd) = cli.cmd.as_mut() else {
        let _ = Cli::command().print_help();
        return;
    };
    if let Some(format) = cli.format.clone() {
        // The root --format is a default for the subcommand's own --format;
        // a subcommand without one rejects it (Typer's main callback).
        if format != "rich" && format != "json" {
            render::usage_error(&format!(
                "Invalid value: Invalid format '{format}'. Choose from: rich, json"
            ));
        }
        if cmd.native_only() {
            cli.json |= format == "json";
        } else {
            match cmd.format_slot() {
                Some(slot) => {
                    slot.get_or_insert(format);
                }
                None => render::usage_error(&format!(
                    "Invalid value for --format: '{name}' has no --format option"
                )),
            }
        }
    }
    if let Some(Cmd::Hook { enforce }) = cli.cmd {
        std::process::exit(symbol_core::hook::run(enforce));
    }

    let wants_json = cli.json
        || cli
            .cmd
            .as_mut()
            .and_then(Cmd::format_slot)
            .is_some_and(|f| f.as_deref() == Some("json"));
    let Err(err) = run(cli) else {
        return;
    };
    match err.downcast_ref::<DaemonUnavailable>() {
        Some(unavailable) if wants_json => {
            println!(
                "{}",
                serde_json::to_string_pretty(&unavailable.to_json()).unwrap_or_default()
            );
        }
        Some(unavailable) => eprintln!("error [daemon_unavailable]: {unavailable}"),
        None => eprintln!("Error: {err:?}"),
    }
    std::process::exit(1);
}

fn exit(code: i32) -> ! {
    use std::io::Write;
    let _ = std::io::stdout().flush();
    std::process::exit(code)
}

fn usage_missing(option: &str) -> ! {
    render::usage_error(&format!("Missing option '{option}'."))
}

/// A value given positionally (this binary's spelling) or as an option
/// (the Python CLI's); not both.
fn either(positional: Option<String>, option: Option<String>, name: &str) -> Option<String> {
    match (positional, option) {
        (Some(_), Some(_)) => render::usage_error(&format!(
            "Got unexpected extra argument (--{name} was also given)"
        )),
        (p, o) => p.or(o),
    }
}

fn run(cli: Cli) -> Result<()> {
    let cmd = cli.cmd.expect("subcommand checked in main");
    let native_json = cli.json;
    if let Cmd::Index { path } = &cmd {
        let root = path.clone().unwrap_or(cli.root.clone()).canonicalize()?;
        return index(&root, native_json);
    }
    if let Cmd::Mcp { root } = &cmd {
        // Python's `mcp -r` and the global --root name the same root; an
        // explicit -r wins.
        let root = if root == Path::new(".") {
            &cli.root
        } else {
            root
        };
        return symbol_core::mcp::serve(&root.canonicalize()?);
    }
    let root = cli.root.canonicalize()?;
    let reg = default_registry(&root);

    match cmd {
        Cmd::Index { .. } | Cmd::Hook { .. } | Cmd::Mcp { .. } => {
            unreachable!("handled before the registry")
        }
        Cmd::Refresh {
            full,
            keep_transactions,
            out,
        } => {
            let out = Out::write(native_json, &out);
            let (idx, refreshed) =
                symbol_core::refresh::refresh(&root, reg, full, keep_transactions)?;
            let mut result = serde_json::json!({
                "status": "ok",
                "source": refreshed.source,
                "symbols": idx.symbols.len(),
                "files": idx.files.len(),
                "transactions_cleared": refreshed.transactions_cleared,
            });
            if out.native_json {
                result["stats"] = idx.stats();
                println!("{result}");
            } else {
                render::refresh_result(&result, out.py_json, out.agent);
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
            out,
        } => {
            if regex && fixed {
                render::print("[red]--regex and --fixed are mutually exclusive[/red]");
                exit(2);
            }
            let out = Out::read(native_json, &out);
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
            if out.native_json {
                println!("{}", serde_json::to_string_pretty(&hits)?);
            } else if out.py_json {
                render::print_json(&Value::Array(hits));
            } else {
                render::search_rich(&patterns, &hits);
            }
        }
        Cmd::Code {
            target,
            no_refs,
            out,
        } => {
            let out = Out::read(native_json, &out);
            let (idx, _) = get_or_build(&root, reg)?;
            if out.native_json {
                let payload = reads::code(&idx, &target, reg)?;
                record_read(&root, &payload);
                println!("{}", serde_json::to_string_pretty(&payload)?);
                return Ok(());
            }
            match reads::lookup_code(&idx, &target, reg) {
                Err(CodeMiss::NotFound(message)) => render::code_not_found(&message, out.py_json),
                Err(CodeMiss::Ambiguous(candidates)) => {
                    render::code_ambiguous(&candidates, out.py_json)
                }
                Ok(hit) => {
                    record_read(&root, &hit);
                    if out.py_json {
                        render::print_json(&hit);
                    } else {
                        render::code_rich(&hit, !no_refs);
                    }
                }
            }
        }
        Cmd::Outline { file, out } => {
            let out = Out::read(native_json, &out);
            let (idx, _) = get_or_build(&root, reg)?;
            // A path on disk is read relative to the project; anything
            // else is a symbol path.
            let arg = Path::new(&file)
                .canonicalize()
                .ok()
                .and_then(|abs| {
                    abs.strip_prefix(&root)
                        .ok()
                        .map(|r| r.to_string_lossy().into_owned())
                })
                .unwrap_or(file);
            let tree = reads::outline(&idx, &arg, reg);
            if out.native_json {
                println!("{}", serde_json::to_string_pretty(&tree)?);
            } else if out.py_json {
                render::print_json(&Value::Array(tree));
            } else {
                render::outline_rich(&arg, &tree);
            }
        }
        Cmd::Callers { name, out } => {
            let out = Out::read(native_json, &out);
            let (idx, _) = get_or_build(&root, reg)?;
            let hits = reads::callers(&idx, &name);
            if out.native_json {
                println!("{}", serde_json::to_string_pretty(&hits)?);
            } else if out.py_json {
                render::print_json(&Value::Array(hits));
            } else {
                render::callers_rich(&name, &hits);
            }
        }
        Cmd::Refs {
            qualified_path,
            verbose,
        } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let result = symbol_core::xref::references(&idx, &qualified_path, reg);
            finish_xref(&result, native_json, verbose, print_refs)?;
        }
        Cmd::Implementers {
            qualified_path,
            verbose,
        } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let result = symbol_core::xref::implementers(&idx, &qualified_path, reg);
            finish_xref(&result, native_json, verbose, print_implementers)?;
        }
        Cmd::Patch {
            file,
            range_arg,
            content_arg,
            range,
            content,
            force,
            dry_run,
            context,
            out,
        } => {
            let out = Out::write(native_json, &out);
            let Some(range) = either(range_arg, range, "range") else {
                usage_missing("--range' / '-r");
            };
            let content = either(content_arg, content, "content").unwrap_or_default();
            patch(&root, &file, &range, &content, force, dry_run, context, out)?;
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
                    exit(1);
                }
            };
            let cache = ReadCache::for_session(&root);
            let result = apply_multi_patch(&file, &parsed, &root, &cache, dry_run, force, 5);
            let failed = matches!(result.status, "error" | "needs_read_confirmation");
            println!("{}", serde_json::to_string_pretty(&result)?);
            if failed {
                exit(1);
            }
        }
        Cmd::DeleteSymbol {
            qualified_path,
            force,
            dry_run,
            context,
            out,
        } => {
            let out = Out::write(native_json, &out);
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = delete_symbol_with_cache(
                &mut idx,
                &qualified_path,
                &root,
                reg,
                force,
                dry_run,
                &ReadCache::for_session(&root),
                context,
            );
            finish_write(&result, out, render::delete_result);
        }
        Cmd::ReplaceSymbol {
            qualified_path,
            content_arg,
            content,
            dry_run,
            out,
        } => {
            let out = Out::write(native_json, &out);
            let Some(content) = either(content_arg, content, "content") else {
                usage_missing("--content");
            };
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = replace_symbol(&mut idx, &qualified_path, &content, &root, reg, dry_run);
            finish_write(&result, out, render::replace_result);
        }
        Cmd::InsertSymbol {
            anchor,
            position_arg,
            content_arg,
            position,
            content,
            no_reindent,
            dry_run,
            context,
            out,
        } => {
            let out = Out::write(native_json, &out);
            let Some(position) = either(position_arg, position, "position") else {
                usage_missing("--position");
            };
            let content = either(content_arg, content, "content").unwrap_or_default();
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = insert_symbol_with_cache(
                &mut idx,
                &anchor,
                &position,
                &content,
                !no_reindent,
                &root,
                reg,
                dry_run,
                &ReadCache::for_session(&root),
                context,
            );
            finish_write(&result, out, render::insert_result);
        }
        Cmd::RenameSymbol {
            qualified_path,
            new_name,
            dry_run,
            out,
        } => {
            let out = Out::write(native_json, &out);
            let (mut idx, _) = get_or_build(&root, reg)?;
            let result = rename_symbol(&mut idx, &qualified_path, &new_name, &root, reg, dry_run);
            finish_write(&result, out, render::rename_result);
        }
        Cmd::Undo { out } => {
            let out = Out::write(native_json, &out);
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
            let value = serde_json::to_value(&result)?;
            if out.native_json {
                println!("{}", serde_json::to_string_pretty(&value)?);
            } else {
                render::undo_result(&value, out.py_json, out.agent);
            }
            if result.status == "error" {
                exit(1);
            }
        }
        Cmd::Loc {
            path,
            out,
            files,
            classify,
            exclude,
            no_vendor_skip,
            no_heuristics,
            no_cache,
            config_dir,
            verbose,
        } => {
            let out = Out::read(native_json, &out);
            let native = native_json || files || classify;
            let mut excludes = exclude;
            if !native {
                // commands/loc.py: the spec's [scanner] scope.
                let scan_root = path.as_deref().unwrap_or(&root);
                excludes.splice(0..0, symbol_core::specs::scanner_excludes(scan_root));
            }
            let project_name = path
                .as_deref()
                .unwrap_or(&cli.root)
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            // The Python command classifies without heuristics and keeps no
            // cache in the project.
            let args = symbol_linguist::loc::LocArgs {
                path: path.unwrap_or(root),
                json: native_json,
                files,
                classify,
                exclude: excludes,
                no_vendor_skip,
                no_heuristics: no_heuristics || !native,
                no_cache: no_cache || !native,
                config_dir,
                verbose,
            };
            if native {
                return symbol_linguist::loc::run(args);
            }
            let report = symbol_linguist::loc::collect(&args)?;
            if out.py_json {
                render::print_json(&render::loc_json(&report)?);
            } else {
                render::loc_rich(&report, &project_name);
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
        Cmd::Map {
            dependents,
            deps,
            depth,
            limit,
            exclude,
            verbose,
        } => {
            let (idx, _) = get_or_build(&root, reg)?;
            let options = symbol_core::map::MapOptions {
                dependents,
                deps,
                depth,
                limit,
                exclude,
                verbose,
                json: native_json,
            };
            if !symbol_core::map::run(&idx, &options) {
                exit(1);
            }
        }
    }
    Ok(())
}

fn index(root: &Path, native_json: bool) -> Result<()> {
    let reg = default_registry(root);
    let t0 = std::time::Instant::now();
    let idx = full_build(root, reg)?;
    let build_ms = t0.elapsed().as_millis();
    let t1 = std::time::Instant::now();
    let path = idx.save()?;
    let save_ms = t1.elapsed().as_millis();
    let stats = idx.stats();
    if native_json {
        println!(
            "{}",
            serde_json::json!({"stats": stats, "path": path, "build_ms": build_ms, "save_ms": save_ms})
        );
        return Ok(());
    }
    let size_kb = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0) as f64 / 1024.0;
    let rel = path.strip_prefix(root).unwrap_or(&path);
    render::print(&format!(
        "[green]indexed[/green] {} files → {} symbols, {} imports, {} refs",
        stats["files"], stats["symbols"], stats["imports"], stats["refs"]
    ));
    render::print(&format!(
        "[dim]build {build_ms} ms  •  save {save_ms} ms  •  {size_kb:.1} KB  •  {}[/dim]",
        rel.display()
    ));
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn patch(
    root: &Path,
    file: &str,
    range: &str,
    content: &str,
    force: bool,
    dry_run: bool,
    context: usize,
    out: Out,
) -> Result<()> {
    // Like the Python CLI, a relative file that exists from the working
    // directory means that file; otherwise it is project-relative.
    let resolved = std::path::absolute(file)
        .ok()
        .filter(|p| Path::new(file).is_relative() && p.exists())
        .map(|p| p.to_string_lossy().into_owned());
    let cache = ReadCache::for_session(root);
    let request = match validate_args(
        resolved.as_deref().unwrap_or(file),
        range,
        content.as_bytes(),
        root,
        force,
    ) {
        Ok(r) => r,
        Err(e) => {
            if out.native_json {
                println!("{}", serde_json::to_string_pretty(&e)?);
            } else {
                let e = serde_json::to_value(&e)?;
                let pre = serde_json::json!({
                    "status": "error",
                    "error_code": e["error_code"],
                    "message": e["message"],
                });
                render::patch_preflight(&pre, out.py_json, out.agent);
            }
            exit(1);
        }
    };
    if let Err(msg) = preflight(&request, &cache) {
        if out.native_json {
            println!(
                "{}",
                serde_json::json!({"status": "needs_read_confirmation", "message": msg})
            );
        } else {
            let source = std::fs::read(&request.file_abs).unwrap_or_default();
            let (s, e) = request.byte_range;
            let current = source
                .get(s..e.min(source.len()))
                .map(|b| String::from_utf8_lossy(b).into_owned())
                .unwrap_or_default();
            let pre = serde_json::json!({
                "status": "needs_read_confirmation",
                "file": request.file_rel,
                "line_range": [request.line_range.0, request.line_range.1],
                "byte_range": [s, e],
                "current_content": current,
            });
            render::patch_preflight(&pre, out.py_json, out.agent);
        }
        exit(2);
    }
    let result = apply_patch(&request, &cache, dry_run, context);
    let failed = result.status == "error";
    if out.native_json {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        render::patch_result(&serde_json::to_value(&result)?, out.py_json, out.agent);
    }
    if failed {
        exit(1);
    }
    Ok(())
}

/// Print a write result in the requested mode; exit 1 on error. The index
/// is not touched: like the Python CLI, the next command's staleness check
/// refreshes (and compacts) it.
fn finish_write(result: &Value, out: Out, human: fn(&Value, bool, bool)) {
    if out.native_json {
        println!(
            "{}",
            serde_json::to_string_pretty(result).unwrap_or_default()
        );
    } else {
        human(result, out.py_json, out.agent);
    }
    if result["status"] == "error" {
        exit(1);
    }
}

/// Record served bodies in the session read cache so a follow-up patch
/// passes preflight — mirrors how commands/code.py records reads.
fn record_read(root: &Path, payload: &Value) {
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
fn finish_xref(result: &Value, json: bool, verbose: bool, print: fn(&Value)) -> Result<()> {
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
        print_not_analyzed(result, verbose);
    }
    if failed {
        exit(1);
    }
    Ok(())
}

fn xref_header(result: &Value) -> String {
    let target = &result["target"];
    format!(
        "{} ({}, {}:{})",
        result["qualified_path"].as_str().unwrap_or(""),
        target["kind"].as_str().unwrap_or(""),
        target["file"].as_str().unwrap_or(""),
        target["line"],
    )
}

/// One note line counting skipped files per reason (the adapter's string,
/// never interpreted here); `-v` lists every file.
fn print_not_analyzed(result: &Value, verbose: bool) {
    let files = result["not_analyzed"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    if files.is_empty() {
        return;
    }
    let mut counts: std::collections::BTreeMap<&str, usize> = Default::default();
    for f in files {
        *counts
            .entry(f["constraint"].as_str().unwrap_or(""))
            .or_default() += 1;
    }
    let mut groups: Vec<(&str, usize)> = counts.into_iter().collect();
    groups.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    let groups: Vec<String> = groups.iter().map(|(c, n)| format!("{c}: {n}")).collect();
    println!(
        "note: {} file(s) not analyzed: {}",
        files.len(),
        groups.join(", ")
    );
    if verbose {
        for f in files {
            println!(
                "  {}  ({})",
                f["file"].as_str().unwrap_or(""),
                f["constraint"].as_str().unwrap_or("")
            );
        }
    }
}

fn print_refs(result: &Value) {
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
}

fn print_implementers(result: &Value) {
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
}
