// Rust port of symbol's GitHub-Linguist subsystem
// (src/wyolet/symbol/shared/linguist/). The Python files are the spec;
// each module mirrors its Python counterpart one-to-one.

pub mod blob;
pub mod cache;
pub mod config;
pub mod detector;
pub mod fnmatch;
pub mod language;
pub mod lines;
pub mod loc;
pub mod re;
pub mod stats;
pub mod strategy;
pub mod walk;
