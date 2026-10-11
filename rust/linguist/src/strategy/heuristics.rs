//! Port of strategy/heuristics.py — WIRED into the chain here (Python
//! vendored heuristics.yml but never added the strategy to
//! `Linguist.strategies`; upstream github-linguist runs it after Manpage).
//!
//! Three latent Python bugs fixed on the way in, per plan:
//! 1. YAML array patterns are unioned as `(?:a)|(?:b)` (Python's to_regex
//!    returns a list that would crash `.match`).
//! 2. Matching is UNANCHORED (Ruby `=~` semantics; Python used re.match).
//! 3. A rule's `language` may be a list — `matches()` treats any listed
//!    language among the candidates as a hit (Python compared the whole
//!    list against candidate names, which never matched).
//!
//! Patterns that neither `regex` nor `fancy-regex` can compile (possessive
//! quantifiers, `\g<name>` subroutine calls) drop their rule, counted —
//! same policy as Python's load_vendor_regex.

use super::Strategy;
use crate::blob::Blob;
use crate::language::{LangId, Registry};
use crate::re::Re;

/// 50 * 1024 — Python slices `blob.data`, a str, so this is CHARS not bytes.
const HEURISTICS_CONSIDER_CHARS: usize = 50 * 1024;

enum Matcher {
    Re(Re),
    And(Vec<Matcher>),
    Negative(Box<Matcher>),
    Always,
}

impl Matcher {
    fn matches(&self, data: &str) -> bool {
        match self {
            Matcher::Re(re) => re.is_match(data),
            Matcher::And(parts) => parts.iter().all(|m| m.matches(data)),
            Matcher::Negative(inner) => !inner.matches(data),
            Matcher::Always => true,
        }
    }
}

struct HRule {
    languages: Vec<String>,
    matcher: Matcher,
}

struct Heuristic {
    extensions: Vec<String>,
    rules: Vec<HRule>,
}

impl Heuristic {
    fn matches(&self, filename: &str, candidate_names: &[&str]) -> bool {
        let f = filename.to_lowercase();
        self.extensions.iter().any(|ext| f.ends_with(ext))
            && self.rules.iter().any(|rule| {
                rule.languages
                    .iter()
                    .any(|l| candidate_names.contains(&l.as_str()))
            })
    }

    fn call(&self, data: &str, reg: &Registry) -> Vec<LangId> {
        for rule in &self.rules {
            if rule.matcher.matches(data) {
                return rule
                    .languages
                    .iter()
                    .filter_map(|name| reg.find_by_name(name))
                    .collect();
            }
        }
        vec![]
    }
}

pub struct HeuristicsStrategy {
    heuristics: Vec<Heuristic>,
    pub dropped_rules: usize,
}

/// `pattern` values in heuristics.yml are a string or a list of strings;
/// lists union into one alternation.
fn union_pattern(v: &serde_yaml::Value) -> Option<String> {
    match v {
        serde_yaml::Value::String(s) => Some(s.clone()),
        serde_yaml::Value::Sequence(seq) => {
            let parts: Option<Vec<String>> = seq
                .iter()
                .map(|p| p.as_str().map(|s| format!("(?:{s})")))
                .collect();
            parts.map(|p| p.join("|"))
        }
        _ => None,
    }
}

impl HeuristicsStrategy {
    pub fn load(yaml: &serde_yaml::Value) -> HeuristicsStrategy {
        let mut dropped = 0usize;

        let mut named: std::collections::HashMap<String, String> = Default::default();
        if let Some(map) = yaml.get("named_patterns").and_then(|v| v.as_mapping()) {
            for (k, v) in map {
                if let (Some(name), Some(pat)) = (k.as_str(), union_pattern(v)) {
                    named.insert(name.to_owned(), pat);
                }
            }
        }

        // parse_rule: and / pattern / negative_pattern / named_pattern / always
        fn parse_matcher(
            named: &std::collections::HashMap<String, String>,
            rule: &serde_yaml::Value,
            dropped: &mut usize,
        ) -> Option<Matcher> {
            let compile = |pat: String, dropped: &mut usize| -> Option<Matcher> {
                match Re::compile(&pat) {
                    Some(re) => Some(Matcher::Re(re)),
                    None => {
                        *dropped += 1;
                        None
                    }
                }
            };
            if let Some(parts) = rule.get("and").and_then(|v| v.as_sequence()) {
                let matchers: Option<Vec<Matcher>> = parts
                    .iter()
                    .map(|p| parse_matcher(named, p, dropped))
                    .collect();
                return matchers.map(Matcher::And);
            }
            if let Some(p) = rule.get("pattern") {
                return compile(union_pattern(p)?, dropped);
            }
            if let Some(p) = rule.get("negative_pattern") {
                return compile(union_pattern(p)?, dropped).map(|m| Matcher::Negative(Box::new(m)));
            }
            if let Some(n) = rule.get("named_pattern").and_then(|v| v.as_str()) {
                return compile(named.get(n)?.clone(), dropped);
            }
            Some(Matcher::Always)
        }

        let mut heuristics = Vec::new();
        if let Some(blocks) = yaml.get("disambiguations").and_then(|v| v.as_sequence()) {
            for block in blocks {
                let extensions: Vec<String> = block
                    .get("extensions")
                    .and_then(|v| v.as_sequence())
                    .map(|seq| {
                        seq.iter()
                            .filter_map(|e| e.as_str().map(str::to_owned))
                            .collect()
                    })
                    .unwrap_or_default();
                let mut rules = Vec::new();
                if let Some(rule_seq) = block.get("rules").and_then(|v| v.as_sequence()) {
                    for rule in rule_seq {
                        let languages: Vec<String> = match rule.get("language") {
                            Some(serde_yaml::Value::String(s)) => vec![s.clone()],
                            Some(serde_yaml::Value::Sequence(seq)) => seq
                                .iter()
                                .filter_map(|l| l.as_str().map(str::to_owned))
                                .collect(),
                            _ => vec![],
                        };
                        if let Some(matcher) = parse_matcher(&named, rule, &mut dropped) {
                            rules.push(HRule { languages, matcher });
                        }
                    }
                }
                heuristics.push(Heuristic { extensions, rules });
            }
        }

        HeuristicsStrategy {
            heuristics,
            dropped_rules: dropped,
        }
    }
}

impl Strategy for HeuristicsStrategy {
    fn call(&self, blob: &Blob, candidates: &[LangId], reg: &Registry) -> Vec<LangId> {
        let data = blob.data();
        let slice = match data.char_indices().nth(HEURISTICS_CONSIDER_CHARS) {
            Some((i, _)) => &data[..i],
            None => data,
        };
        let candidate_names: Vec<&str> = candidates
            .iter()
            .map(|&id| reg.get(id).name.as_str())
            .collect();

        for heuristic in &self.heuristics {
            if heuristic.matches(&blob.name, &candidate_names) {
                // Python: `return heuristic.call(data) or []` — first
                // extension+language match wins, even if its result is empty.
                return heuristic.call(slice, reg);
            }
        }
        vec![]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{load, ConfigSource};
    use std::path::Path;

    fn setup() -> (Registry, HeuristicsStrategy) {
        let cfg = load(&ConfigSource::Embedded).unwrap();
        let reg = Registry::load(&cfg).unwrap();
        let h = HeuristicsStrategy::load(&cfg.heuristics);
        (reg, h)
    }

    fn run(
        reg: &Registry,
        h: &HeuristicsStrategy,
        name: &str,
        content: &str,
        cands: &[&str],
    ) -> Vec<String> {
        let blob = Blob::from_bytes(
            Path::new(name),
            content.as_bytes().to_vec(),
            false,
            content.len() as u64,
        );
        let cand_ids: Vec<LangId> = cands.iter().map(|n| reg.find_by_name(n).unwrap()).collect();
        h.call(&blob, &cand_ids, reg)
            .into_iter()
            .map(|id| reg.get(id).name.clone())
            .collect()
    }

    #[test]
    fn loads_with_bounded_drops() {
        let (_, h) = setup();
        assert!(!h.heuristics.is_empty());
        assert!(
            h.dropped_rules <= 10,
            "dropped {} heuristic rules — update-linguist regression?",
            h.dropped_rules
        );
    }

    #[test]
    fn disambiguates_h_objc() {
        let (reg, h) = setup();
        let objc = "#import <Foundation/Foundation.h>\n@interface Foo : NSObject\n@end\n";
        let got = run(&reg, &h, "x.h", objc, &["C", "C++", "Objective-C"]);
        assert_eq!(got, vec!["Objective-C"]);
    }

    #[test]
    fn disambiguates_pl_perl_prolog() {
        let (reg, h) = setup();
        let prolog = ":- module(foo, [bar/1]).\nbar(X) :- X > 0.\n";
        let got = run(&reg, &h, "x.pl", prolog, &["Perl", "Prolog", "Raku"]);
        assert_eq!(got, vec!["Prolog"]);
    }
}
