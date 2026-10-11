use std::fs;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;

use anyhow::{Context, Result};

use crate::schema::Candidate;

pub fn run(input: &Path, out: &Path, op: &str) -> Result<()> {
    if let Some(parent) = out.parent() {
        fs::create_dir_all(parent)?;
    }
    let reader = BufReader::new(fs::File::open(input)?);
    let mut writer = BufWriter::new(fs::File::create(out)?);
    let mut kept = 0usize;
    for (line_number, line) in reader.lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let candidate: Candidate = serde_json::from_str(&line)
            .with_context(|| format!("parse candidate line {}", line_number + 1))?;
        if primary_op(&candidate.operation_hints) != Some(op) {
            continue;
        }
        serde_json::to_writer(&mut writer, &candidate)?;
        writer.write_all(b"\n")?;
        kept += 1;
    }
    writer.flush()?;
    println!("wrote {kept} candidates for {op} to {}", out.display());
    Ok(())
}

pub fn primary_op(hints: &[String]) -> Option<&'static str> {
    const ORDER: &[&str] = &[
        "multi-patch",
        "rename-symbol",
        "replace-symbol",
        "delete-symbol",
        "insert-symbol",
        "patch",
    ];
    ORDER.iter().copied().find(|op| hints.iter().any(|hint| hint == op))
}

#[cfg(test)]
mod tests {
    use super::primary_op;

    #[test]
    fn prioritizes_specific_operations_over_patch() {
        assert_eq!(primary_op(&["patch".into(), "rename-symbol".into()]), Some("rename-symbol"));
        assert_eq!(primary_op(&["patch".into(), "multi-patch".into()]), Some("multi-patch"));
        assert_eq!(primary_op(&["patch".into()]), Some("patch"));
    }
}
