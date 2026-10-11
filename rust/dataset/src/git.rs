use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use anyhow::{bail, Context, Result};

use crate::schema::{Corpus, CorpusManifest};

pub fn load_manifest(path: &Path) -> Result<CorpusManifest> {
    let manifest: CorpusManifest = serde_json::from_slice(&fs::read(path)?)
        .with_context(|| format!("parse corpus manifest {}", path.display()))?;
    if manifest.version != 1 {
        bail!("corpus manifest version must be 1");
    }
    Ok(manifest)
}

pub fn repo_path(cache: &Path, corpus: &Corpus) -> PathBuf {
    cache.join(format!("{}.git", corpus.id))
}

pub fn fetch_corpora(manifest_path: &Path, cache: &Path, update: bool) -> Result<()> {
    let manifest = load_manifest(manifest_path)?;
    fs::create_dir_all(cache)?;
    for corpus in &manifest.corpora {
        let repo = repo_path(cache, corpus);
        if repo.is_dir() {
            if update {
                println!("updating {}", corpus.id);
                checked(
                    Command::new("git")
                        .args(["--git-dir"])
                        .arg(&repo)
                        .args(["fetch", "--prune", "origin"]),
                    "git fetch",
                )?;
            } else {
                println!("present  {}", corpus.id);
            }
            continue;
        }
        println!("cloning  {} ({})", corpus.id, corpus.url);
        checked(
            Command::new("git")
                .args(["clone", "--bare", &corpus.url])
                .arg(&repo),
            "git clone --bare",
        )?;
    }
    Ok(())
}

pub fn git_output(repo: &Path, args: &[String]) -> Result<String> {
    let output = Command::new("git")
        .arg("--git-dir")
        .arg(repo)
        .args(args)
        .output()
        .with_context(|| format!("run git {args:?}"))?;
    if !output.status.success() {
        bail!(
            "git {:?} failed: {}",
            args,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

pub fn clone_at(repo: &Path, parent: &str, target: &Path) -> Result<()> {
    checked(
        Command::new("git")
            .args(["clone", "--shared", "--no-checkout"])
            .arg(repo)
            .arg(target),
        "git clone --shared",
    )?;
    checked(
        Command::new("git")
            .arg("-C")
            .arg(target)
            .args(["checkout", "--detach", parent]),
        "git checkout parent",
    )?;
    Ok(())
}

pub fn checked(command: &mut Command, label: &str) -> Result<Output> {
    let output = command.output().with_context(|| format!("run {label}"))?;
    if !output.status.success() {
        bail!(
            "{label} failed ({}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(output)
}
