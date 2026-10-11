//! Embeds the package spec catalog (data/spec.toml + data/specs/*/spec.toml)
//! so the binary applies per-package specs without the Python package.

use std::fmt::Write;
use std::path::Path;

fn main() {
    let data = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src/wyolet/symbol/data");
    println!("cargo:rerun-if-changed={}", data.display());

    let mut files = vec![("spec.toml".to_owned(), data.join("spec.toml"))];
    let mut packages: Vec<_> = std::fs::read_dir(data.join("specs"))
        .expect("data/specs")
        .filter_map(Result::ok)
        .map(|e| e.path().join("spec.toml"))
        .filter(|p| p.is_file())
        .collect();
    packages.sort();
    for path in packages {
        let dir = path.parent().and_then(Path::file_name).unwrap();
        files.push((format!("specs/{}/spec.toml", dir.to_string_lossy()), path));
    }

    let mut out = String::from("pub const SPEC_FILES: &[(&str, &str)] = &[\n");
    for (rel, path) in files {
        let abs = path.canonicalize().unwrap();
        writeln!(
            out,
            "    ({rel:?}, include_str!({:?})),",
            abs.display().to_string()
        )
        .unwrap();
    }
    out.push_str("];\n");
    let dest = Path::new(&std::env::var("OUT_DIR").unwrap()).join("embedded_specs.rs");
    std::fs::write(dest, out).unwrap();
}
