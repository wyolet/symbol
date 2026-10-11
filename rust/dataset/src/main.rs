mod compose;
mod exercise;
mod export;
mod git;
mod mine;
mod partition;
mod schema;
mod verify;

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Clone missing corpus mirrors and optionally update existing mirrors
    Fetch {
        manifest: PathBuf,
        #[arg(long, default_value = ".tmp/corpora")]
        repo_cache: PathBuf,
        #[arg(long)]
        update: bool,
    },
    /// Mine small source commits into deterministic candidate packets
    Mine {
        manifest: PathBuf,
        #[arg(long, default_value = ".tmp/corpora")]
        repo_cache: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        limit_per_corpus: Option<usize>,
    },
    /// Partition candidate packets by their primary operation hint
    Partition {
        input: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        op: String,
    },
    /// Compose candidate packets into tasks with Grok structured output
    Compose {
        input: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long, default_value = "grok")]
        grok_bin: PathBuf,
        #[arg(long, default_value = "grok-4.5")]
        model: String,
        #[arg(long, default_value = ".tmp/dataset-model-cache")]
        cache_dir: PathBuf,
        #[arg(long)]
        limit: Option<usize>,
        #[arg(long)]
        limit_per_corpus: Option<usize>,
        #[arg(long)]
        require_hint: Option<String>,
    },
    /// Let Grok execute natural tasks through a Rust-mediated symbol-rs loop
    Exercise {
        manifest: PathBuf,
        input: PathBuf,
        #[arg(long, default_value = ".tmp/corpora")]
        repo_cache: PathBuf,
        #[arg(long)]
        symbol_bin: PathBuf,
        #[arg(long, default_value = "grok")]
        grok_bin: PathBuf,
        #[arg(long, default_value = "grok-4.5")]
        model: String,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        verified_input: Option<PathBuf>,
        #[arg(long, default_value_t = 0)]
        skip: usize,
        #[arg(long)]
        limit: Option<usize>,
        #[arg(long)]
        keep_failures: bool,
    },
    /// Replay composed tasks on parent commits and compare exact child files
    Verify {
        manifest: PathBuf,
        input: PathBuf,
        #[arg(long, default_value = ".tmp/corpora")]
        repo_cache: PathBuf,
        #[arg(long)]
        symbol_bin: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        run_checks: bool,
        #[arg(long)]
        limit: Option<usize>,
    },
    /// Export exact Grok traces into external-corpus harness scenarios
    Export {
        input: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long, default_value = "corpora.json")]
        corpora_manifest: String,
    },
    #[command(hide = true)]
    Proxy {
        #[arg(long)]
        actual: PathBuf,
        #[arg(long)]
        log: PathBuf,
        #[arg(last = true)]
        args: Vec<String>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Fetch {
            manifest,
            repo_cache,
            update,
        } => git::fetch_corpora(&manifest, &repo_cache, update),
        Commands::Mine {
            manifest,
            repo_cache,
            out,
            limit_per_corpus,
        } => mine::run(&manifest, &repo_cache, &out, limit_per_corpus),
        Commands::Partition { input, out, op } => partition::run(&input, &out, &op),
        Commands::Compose {
            input,
            out,
            grok_bin,
            model,
            cache_dir,
            limit,
            limit_per_corpus,
            require_hint,
        } => compose::run(compose::RunConfig {
            input: &input,
            out: &out,
            grok_bin: &grok_bin,
            model: &model,
            cache_dir: &cache_dir,
            limit,
            limit_per_corpus,
            require_hint: require_hint.as_deref(),
        }),
        Commands::Exercise {
            manifest,
            input,
            repo_cache,
            symbol_bin,
            grok_bin,
            model,
            out,
            verified_input,
            skip,
            limit,
            keep_failures,
        } => exercise::run(
            &manifest,
            &input,
            &repo_cache,
            &symbol_bin,
            &grok_bin,
            &model,
            &out,
            verified_input.as_deref(),
            skip,
            limit,
            keep_failures,
        ),
        Commands::Verify {
            manifest,
            input,
            repo_cache,
            symbol_bin,
            out,
            run_checks,
            limit,
        } => verify::run(
            &manifest,
            &input,
            &repo_cache,
            &symbol_bin,
            &out,
            run_checks,
            limit,
        ),
        Commands::Export {
            input,
            out,
            corpora_manifest,
        } => export::run(&input, &out, &corpora_manifest),
        Commands::Proxy { actual, log, args } => {
            let code = exercise::proxy(&actual, &log, &args)?;
            std::process::exit(code);
        }
    }
}
