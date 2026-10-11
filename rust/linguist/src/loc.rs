//! `symbol loc` as a library — shared by the standalone `symbol-linguist`
//! binary and the `symbol-rs loc` subcommand (one product, one code path).

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Instant;

use anyhow::{Context, Result};
use clap::Parser;
use rayon::prelude::*;

use crate::blob::Blob;
use crate::cache::{effective_fingerprint, Cache, FileRec};
use crate::config::{build_vendor_regex, load, ConfigSource};
use crate::detector::Detector;
use crate::fnmatch::GlobPat;
use crate::language::Registry;
use crate::stats::Statistics;
use crate::walk::{walk, WalkOptions};

/// Rust port of `symbol loc` — GitHub-Linguist language detection + LOC.
#[derive(Parser, Debug)]
#[command(version, about)]
pub struct LocArgs {
    /// Project root to scan
    #[arg(default_value = ".")]
    pub path: PathBuf,

    /// JSON output (same shape as `symbol loc --format json`)
    #[arg(long)]
    pub json: bool,

    /// Include per-file records in JSON output
    #[arg(long)]
    pub files: bool,

    /// Classification only: print "<rel_path>\t<language>" per file
    #[arg(long)]
    pub classify: bool,

    /// Exclude glob (Python fnmatch semantics), repeatable
    #[arg(long)]
    pub exclude: Vec<String>,

    /// Do not prune vendored dirs/files (vendor.yml)
    #[arg(long)]
    pub no_vendor_skip: bool,

    /// Disable the heuristics strategy — chain identical to the Python port
    #[arg(long)]
    pub no_heuristics: bool,

    /// Skip reading and writing .symbol/linguist.msgpack.zst
    #[arg(long)]
    pub no_cache: bool,

    /// Directory holding the six linguist YAMLs (default: embedded copies,
    /// or $SYMBOL_LINGUIST_CONFIG_DIR)
    #[arg(long)]
    pub config_dir: Option<PathBuf>,

    /// Log dropped-pattern counts, cache stats, timings to stderr
    #[arg(short, long)]
    pub verbose: bool,
}

/// One classified file, in walk order.
struct Outcome {
    pub rel: String,
    pub abs: String,
    pub mtime_ns: i64,
    pub size: u64,
    pub lang_name: Option<String>,
    pub loc: u64,
    pub sloc: u64,
}

pub fn run(args: LocArgs) -> Result<()> {
    let t0 = Instant::now();

    let root = std::fs::canonicalize(&args.path)
        .with_context(|| format!("resolving {}", args.path.display()))?;

    let source = ConfigSource::resolve(args.config_dir.clone());
    let configs = load(&source)?;
    let reg = Registry::load(&configs)?;
    let detector = Detector::new(&configs, reg, !args.no_heuristics);

    let (vendor, vendor_dropped) = if args.no_vendor_skip {
        (None, 0)
    } else {
        build_vendor_regex(&configs.vendor_patterns)
    };
    let exclude: Vec<GlobPat> = args
        .exclude
        .iter()
        .filter_map(|p| GlobPat::new(p))
        .collect();

    let t_setup = t0.elapsed();
    let walked = walk(
        &root,
        &WalkOptions {
            exclude: &exclude,
            vendor: vendor.as_ref(),
        },
    );
    let t_walk = t0.elapsed() - t_setup;

    let fp = effective_fingerprint(configs.fingerprint, !args.no_heuristics);
    let mut cache = if args.no_cache {
        None
    } else {
        Some(Cache::load(&root, fp))
    };

    // Split into cache hits and files needing classification.
    enum Work<'a> {
        Hit(&'a crate::walk::WalkedFile, Option<String>, u64, u64),
        Todo(&'a crate::walk::WalkedFile),
    }
    let work: Vec<Work> = walked
        .iter()
        .map(|f| {
            match cache.as_mut().and_then(|c| {
                c.get(&f.rel, f.mtime_ns, f.size)
                    .map(|rec| (rec.2.clone(), rec.3, rec.4))
            }) {
                Some((lang, loc, sloc)) => Work::Hit(f, lang, loc, sloc),
                None => Work::Todo(f),
            }
        })
        .collect();

    // Classify misses in parallel; everything stays in walk order.
    let outcomes: Vec<Outcome> = work
        .par_iter()
        .map(|w| match w {
            Work::Hit(f, lang, loc, sloc) => Outcome {
                rel: f.rel.clone(),
                abs: f.abs.to_string_lossy().into_owned(),
                mtime_ns: f.mtime_ns,
                size: f.size,
                lang_name: lang.clone(),
                loc: *loc,
                sloc: *sloc,
            },
            Work::Todo(f) => {
                let (lang_name, loc, sloc) = match Blob::from_path(&f.abs, f.is_symlink, f.size) {
                    Ok(blob) => match detector.detect(&blob) {
                        Some(id) => (
                            Some(detector.reg.get(id).name.clone()),
                            blob.loc(),
                            blob.sloc(),
                        ),
                        None => (None, 0, 0),
                    },
                    // Python would crash on unreadable files (broken
                    // symlinks); we skip them as gated-out instead.
                    Err(_) => (None, 0, 0),
                };
                Outcome {
                    rel: f.rel.clone(),
                    abs: f.abs.to_string_lossy().into_owned(),
                    mtime_ns: f.mtime_ns,
                    size: f.size,
                    lang_name,
                    loc,
                    sloc,
                }
            }
        })
        .collect();
    let t_classify = t0.elapsed() - t_walk - t_setup;

    // Aggregate in walk order (Python statistics dict = first-seen order).
    let mut stats = Statistics::default();
    for o in &outcomes {
        if let Some(name) = &o.lang_name {
            if let Some(id) = detector.reg.find_by_name(name) {
                stats.add(&detector.reg, id, o.abs.clone(), o.loc, o.sloc, o.size);
            }
        }
    }
    stats.calculate_percentages();

    if let Some(c) = &cache {
        let fresh: HashMap<String, FileRec> = outcomes
            .iter()
            .map(|o| {
                (
                    o.rel.clone(),
                    FileRec(o.mtime_ns, o.size, o.lang_name.clone(), o.loc, o.sloc),
                )
            })
            .collect();
        if let Err(e) = c.save(fresh) {
            eprintln!("warning: could not write cache: {e}");
        }
    }

    if args.verbose {
        let (hits, misses) = cache.as_ref().map(|c| (c.hits, c.misses)).unwrap_or((0, 0));
        eprintln!(
            "setup {:?}  walk {:?} ({} files)  classify {:?}  cache {hits} hits / {misses} misses",
            t_setup,
            t_walk,
            walked.len(),
            t_classify,
        );
        eprintln!(
            "vendor patterns dropped: {vendor_dropped}  heuristic rules dropped: {}",
            detector.heuristic_rules_dropped
        );
    }

    if args.classify {
        output_classify(&outcomes, args.json);
    } else if args.json {
        output_json(&stats, &outcomes, args.files)?;
    } else {
        output_table(&stats, &root);
    }
    Ok(())
}

fn output_classify(outcomes: &[Outcome], json: bool) {
    let mut classified: Vec<(&str, &str)> = outcomes
        .iter()
        .filter_map(|o| o.lang_name.as_deref().map(|l| (o.rel.as_str(), l)))
        .collect();
    classified.sort();
    if json {
        let map: serde_json::Map<String, serde_json::Value> = classified
            .into_iter()
            .map(|(p, l)| (p.to_owned(), serde_json::Value::String(l.to_owned())))
            .collect();
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({ "files": map })).unwrap()
        );
    } else {
        for (path, lang) in classified {
            println!("{path}\t{lang}");
        }
    }
}

/// Same JSON shape as commands/loc.py `--format json`.
fn output_json(stats: &Statistics, outcomes: &[Outcome], with_files: bool) -> Result<()> {
    let languages: Vec<_> = stats.iter().map(|s| s.dict()).collect();
    let (mut sloc, mut loc, mut files, mut size) = (0u64, 0u64, 0u64, 0u64);
    let mut code_languages = 0u64;
    for s in stats.iter() {
        sloc += s.sloc;
        loc += s.loc;
        files += s.files;
        size += s.size;
        if s.lang_type == "programming" {
            code_languages += 1;
        }
    }

    #[derive(serde::Serialize)]
    struct BigFile<'a> {
        path: &'a str,
        lines: u64,
        sloc: u64,
        size: u64,
        language: &'a str,
    }
    let mut code_files: Vec<BigFile> = stats
        .iter()
        .filter(|s| s.lang_type == "programming")
        .flat_map(|s| {
            s.file_stats.iter().map(|f| BigFile {
                path: &f.path,
                lines: f.lines,
                sloc: f.sloc,
                size: f.size,
                language: &s.name,
            })
        })
        .collect();
    code_files.sort_by_key(|f| std::cmp::Reverse(f.lines));
    code_files.truncate(10);

    let mut data = serde_json::json!({
        "summary": {
            "sloc": sloc,
            "loc": loc,
            "files": files,
            "size": size,
            "languages": languages.len(),
            "code_languages": code_languages,
        },
        "languages": languages,
        "biggest_files": code_files,
    });
    if with_files {
        #[derive(serde::Serialize)]
        struct FileOut<'a> {
            path: &'a str,
            language: Option<&'a str>,
            lines: u64,
            sloc: u64,
            size: u64,
        }
        let all: Vec<FileOut> = outcomes
            .iter()
            .map(|o| FileOut {
                path: &o.rel,
                language: o.lang_name.as_deref(),
                lines: o.loc,
                sloc: o.sloc,
                size: o.size,
            })
            .collect();
        data["files"] = serde_json::to_value(all)?;
    }
    println!("{}", serde_json::to_string_pretty(&data)?);
    Ok(())
}

fn output_table(stats: &Statistics, root: &std::path::Path) {
    let name = root
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| root.display().to_string());
    println!("symbol-linguist — {name}/");
    println!();
    if stats.is_empty() {
        println!("  (no recognized source files)");
        return;
    }
    let mut rows: Vec<_> = stats.iter().collect();
    rows.sort_by_key(|s| std::cmp::Reverse(s.sloc));

    let (mut tsloc, mut tloc, mut tfiles, mut tsize) = (0u64, 0u64, 0u64, 0u64);
    println!(
        "{:<28} {:<12} {:>7} {:>12} {:>12} {:>10} {:>7}",
        "Language", "Type", "Files", "SLOC", "Lines", "KB", "%"
    );
    for s in &rows {
        println!(
            "{:<28} {:<12} {:>7} {:>12} {:>12} {:>10} {:>6.1}%",
            s.name,
            s.lang_type,
            s.files,
            s.sloc,
            s.loc,
            s.size / 1024,
            s.percentage_lines,
        );
        tsloc += s.sloc;
        tloc += s.loc;
        tfiles += s.files;
        tsize += s.size;
    }
    println!(
        "{:<28} {:<12} {:>7} {:>12} {:>12} {:>10}",
        "Total",
        "",
        tfiles,
        tsloc,
        tloc,
        tsize / 1024
    );
    println!();
    println!(
        "{} sloc  {} lines  {} blank  {} files  {} langs",
        tsloc,
        tloc,
        tloc - tsloc,
        tfiles,
        rows.len()
    );
}
