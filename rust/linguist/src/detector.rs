//! Port of shared/linguist/linguist.py `Linguist.detect` + `_best_candidate`
//! — the strategy chain with narrow-then-decide, popular preference, and the
//! `_PREFERRED` tiebreak.

use crate::blob::Blob;
use crate::config::Configs;
use crate::language::{LangId, Registry};
use crate::strategy::{
    extension::ExtensionStrategy, filename::FilenameStrategy, heuristics::HeuristicsStrategy,
    manpage::ManpageStrategy, modeline::ModeLineStrategy, shebang::ShebangStrategy,
    xml::XMLStrategy, Strategy,
};

/// linguist.py `_PREFERRED` — well-known ambiguous extensions where the
/// common language always wins.
const PREFERRED: &[(&str, &str)] = &[
    (".md", "Markdown"),
    (".ts", "TypeScript"),
    (".tsx", "TSX"),
    (".yml", "YAML"),
    (".yaml", "YAML"),
    (".json", "JSON"),
    (".m", "Objective-C"),
    (".pl", "Perl"),
    (".r", "R"),
    (".d", "D"),
    (".v", "Verilog"),
    (".fs", "F#"),
    (".cls", "Apex"),
    (".h", "C"),
];

fn type_priority(t: &str) -> u8 {
    match t {
        "programming" => 0,
        "markup" => 1,
        "prose" => 2,
        "data" => 3,
        _ => 99,
    }
}

pub struct Detector {
    pub reg: Registry,
    strategies: Vec<Box<dyn Strategy + Sync + Send>>,
    pub heuristic_rules_dropped: usize,
}

impl Detector {
    pub fn new(configs: &Configs, reg: Registry, heuristics_enabled: bool) -> Detector {
        let mut strategies: Vec<Box<dyn Strategy + Sync + Send>> = vec![
            Box::new(ModeLineStrategy),
            Box::new(FilenameStrategy),
            Box::new(ShebangStrategy),
            Box::new(ExtensionStrategy::new(configs.generic_extensions.clone())),
            Box::new(XMLStrategy),
            Box::new(ManpageStrategy),
        ];
        let mut dropped = 0;
        if heuristics_enabled {
            // Upstream github-linguist position: after Manpage. Python's port
            // never wired this strategy in at all — `--no-heuristics`
            // reproduces that chain exactly.
            let h = HeuristicsStrategy::load(&configs.heuristics);
            dropped = h.dropped_rules;
            strategies.push(Box::new(h));
        }
        Detector {
            reg,
            strategies,
            heuristic_rules_dropped: dropped,
        }
    }

    /// linguist.py detect(): None = gated out (symlink/binary/empty);
    /// Unknown is a real result and IS counted, matching Python where
    /// detect_directory keeps truthy Unknown.
    pub fn detect(&self, blob: &Blob) -> Option<LangId> {
        if blob.is_symlink || blob.likely_binary() || blob.binary() || blob.empty() {
            return None;
        }

        let mut languages: Vec<LangId> = vec![];
        for strategy in &self.strategies {
            let candidates = strategy.call(blob, &languages, &self.reg);
            if candidates.len() == 1 {
                return Some(candidates[0]);
            }
            if candidates.len() > 1 {
                languages = candidates;
            }
        }

        if languages.is_empty() {
            return Some(self.reg.unknown);
        }

        // Prefer popular languages (linguist.py:94-98)
        for &id in &languages {
            let lang = self.reg.get(id);
            if lang.popular {
                return Some(id);
            }
            if self.reg.popular_names.contains(&lang.group) {
                return Some(self.reg.find_by_name(&lang.group).unwrap_or(id));
            }
        }

        Some(self.best_candidate(languages, blob))
    }

    /// _best_candidate: _PREFERRED table, then stable sort by
    /// (fewer extensions = more specific, programming > markup > prose > data).
    fn best_candidate(&self, mut languages: Vec<LangId>, blob: &Blob) -> LangId {
        let ext = blob.extname().to_lowercase();
        if let Some(&(_, want)) = PREFERRED.iter().find(|&&(e, _)| e == ext) {
            if let Some(&id) = languages.iter().find(|&&id| self.reg.get(id).name == want) {
                return id;
            }
        }
        languages.sort_by_key(|&id| {
            let lang = self.reg.get(id);
            (lang.extensions.len(), type_priority(&lang.lang_type))
        });
        languages[0]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{load, ConfigSource};
    use std::path::Path;

    fn detector(heuristics: bool) -> Detector {
        let cfg = load(&ConfigSource::Embedded).unwrap();
        let reg = Registry::load(&cfg).unwrap();
        Detector::new(&cfg, reg, heuristics)
    }

    fn name_of(d: &Detector, file: &str, content: &[u8]) -> Option<String> {
        let blob = Blob::from_bytes(
            Path::new(file),
            content.to_vec(),
            false,
            content.len() as u64,
        );
        d.detect(&blob).map(|id| d.reg.get(id).name.clone())
    }

    #[test]
    fn golden_classification_no_heuristics() {
        let d = detector(false);
        assert_eq!(name_of(&d, "a.py", b"print(1)\n"), Some("Python".into()));
        assert_eq!(name_of(&d, "a.rs", b"fn main() {}\n"), Some("Rust".into()));
        assert_eq!(name_of(&d, "a.go", b"package main\n"), Some("Go".into()));
        // _PREFERRED: .h → C without content heuristics
        assert_eq!(name_of(&d, "a.h", b"int x;\n"), Some("C".into()));
        assert_eq!(name_of(&d, "a.md", b"# hi\n"), Some("Markdown".into()));
        // filename strategy
        assert_eq!(
            name_of(&d, "Makefile", b"all:\n\ttrue\n"),
            Some("Makefile".into())
        );
        assert_eq!(
            name_of(&d, "Dockerfile", b"FROM x\n"),
            Some("Dockerfile".into())
        );
        // shebang decides for extensionless scripts
        assert_eq!(
            name_of(&d, "script", b"#!/usr/bin/env python3\nprint(1)\n"),
            Some("Python".into())
        );
        // xml strategy
        assert_eq!(
            name_of(&d, "noext", b"<?xml version=\"1.0\"?>\n<a/>\n"),
            Some("XML".into())
        );
        // manpage
        assert_eq!(
            name_of(&d, "foo.1", b".TH FOO 1\n"),
            Some("Roff Manpage".into())
        );
        // unknown extension → Unknown (counted)
        assert_eq!(name_of(&d, "a.zzznope", b"data\n"), Some("Unknown".into()));
        // gates → None
        assert_eq!(name_of(&d, "a.py", b""), None);
        assert_eq!(name_of(&d, "a.py", b"x\x00y"), None);
        // modeline beats extension (runs first, decides)
        assert_eq!(
            name_of(&d, "weird.txt", b"; -*- mode: ruby -*-\ncode\n"),
            Some("Ruby".into())
        );
    }

    #[test]
    fn heuristics_changes_dot_h() {
        let d = detector(true);
        let objc = b"#import <Foundation/Foundation.h>\n@interface Foo : NSObject\n@end\n";
        assert_eq!(name_of(&d, "a.h", objc), Some("Objective-C".into()));
        // plain C header still lands on C via heuristics' C++ negative rules
        // falling through to _PREFERRED
        let plain = b"#include <stdio.h>\nint x;\n";
        let got = name_of(&d, "b.h", plain).unwrap();
        assert!(got == "C" || got == "C++", "got {got}");
    }

    #[test]
    fn symlink_gated() {
        let d = detector(false);
        let blob = Blob::from_bytes(Path::new("a.py"), b"print(1)".to_vec(), true, 8);
        assert_eq!(d.detect(&blob), None);
    }
}
