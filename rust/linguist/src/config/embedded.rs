//! Embedded fallback copies of the six linguist YAMLs, baked in from the
//! Python package's config dir at build time (cargo tracks include_str!
//! files for rebuilds). Runtime `--config-dir` / env dir takes precedence.

const LANGUAGES: &str =
    include_str!("../../../../src/wyolet/symbol/shared/linguist/config/languages.yml");
const HEURISTICS: &str =
    include_str!("../../../../src/wyolet/symbol/shared/linguist/config/heuristics.yml");
const VENDOR: &str =
    include_str!("../../../../src/wyolet/symbol/shared/linguist/config/vendor.yml");
const DOCUMENTATION: &str =
    include_str!("../../../../src/wyolet/symbol/shared/linguist/config/documentation.yml");
const POPULAR: &str =
    include_str!("../../../../src/wyolet/symbol/shared/linguist/config/popular.yml");
const GENERIC: &str =
    include_str!("../../../../src/wyolet/symbol/shared/linguist/config/generic.yml");

pub fn get(name: &str) -> &'static str {
    match name {
        "languages.yml" => LANGUAGES,
        "heuristics.yml" => HEURISTICS,
        "vendor.yml" => VENDOR,
        "documentation.yml" => DOCUMENTATION,
        "popular.yml" => POPULAR,
        "generic.yml" => GENERIC,
        _ => panic!("unknown config file: {name}"),
    }
}
