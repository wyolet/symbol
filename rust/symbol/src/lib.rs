// Rust port of symbol's language-neutral core. The Python modules are the
// spec (shared/symbol_index.py, reads/, writes/); language analysis runs in
// out-of-process native-AST daemons (pyscan for Python, go-scan for Go)
// speaking the schemas/symbol.rpc.schema.json contract over stdio.

pub mod adapters;
pub mod hook;
pub mod index;
pub mod map;
pub mod pyjson;
pub mod pyscan;
pub mod reads;
pub mod render;
pub mod rpc;
pub mod specs;
pub mod writes;
pub mod xref;
