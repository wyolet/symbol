# symbol-core (Rust)

Rust port of symbol's language-neutral core: the symbol index, the read
surface (search / code / outline / callers), and the write surface (patch /
delete-symbol / replace-symbol / rename-symbol / undo) behind the `symbol-rs`
binary. Language analysis never happens in-process — it runs in native-AST
daemons speaking newline-delimited JSON-RPC 2.0 over stdio
(schemas/symbol.rpc.schema.json):

- **Python** → `wyolet.symbol.adapters.pyscan` (new; ~150-line stdio socket
  around the existing `PythonAstAdapter` — Python code is analyzed by
  Python's own `ast`, exactly as before)
- **Go** → the existing `daemon/go-scan` binary, unchanged

Daemon discovery: `SYMBOL_PYSCAN` / `SYMBOL_GOSCAN` (full command override),
`SYMBOL_REPO` (checkout providing both), else the crate's own repo (dev mode).

## Interop guarantees (verified end-to-end)

- **Index**: `.symbol/symbol_index.msgpack.zst` is byte-compatible v6 —
  Python loads Rust-built indexes and vice versa. (Gotcha encoded in
  `index.rs`: Python's `zstandard.decompress()` requires the frame content
  size, so saves use `zstd::bulk::compress`.)
- **Transactions**: `.symbol/transactions/<id>/manifest.json` matches
  Python's format — `symbol undo` (Python) rolls back Rust-committed writes
  and `symbol-rs undo` rolls back Python's.
- **Read cache**: `.symbol/cache/sessions/<SYMBOL_SESSION_ID>.json`, same
  JSON shape as caches/disk.py, so patch preflight works across both CLIs
  in one agent session.

## Documented divergences (tonight's cut lines)

- **rename-symbol**: Python engine kinds run the REAL v2 receiver-aware
  analysis — pyscan loads the shared on-disk index, builds
  RenamerIndexQuery, and executes rename_member/rename_module_binding;
  verified byte-identical to Python including discriminator skips. Go
  engine kinds remain tier-1 until go-scan's rename RPC is mirrored.
  Results carry `"engine": "v2"` or `"engine": "tier1"`.
- **insert-symbol not ported** (its reindent logic is `ast`-bound in
  Python; belongs in the daemon as an RPC first).
- **audit / map / analyze / MCP server not ported** — out of scope by
  design; they're the Python-specific engine documented in CLAUDE.md.
- Index build applies linguist classification (heuristics off = Python
  chain) + the adapter gate, but not `[scanner]`/`[checker]` spec excludes.
- Blank-gap normalization after delete/replace goes through the daemon
  (`normalize_blank_gaps` RPC), preserving the PEP 8 behavior for Python;
  other languages skip it.
- Go indexing works through go-scan but `module_prefix` is currently left
  to the daemon default; Go qualified paths may differ from a Python-core
  build until that's mirrored.

## Layout

```
src/rpc.rs        JSON-RPC stdio client (go_ast transport, reused)
src/adapters.rs   registry + daemon adapters + wire types (protocols/types.py)
src/index.rs      shared/symbol_index.py — v6 format, staleness, refresh
src/reads.rs      reads/ — search, code, outline, callers
src/writes/       writes/ — patch, tx+undo, read_cache, content, ops, rename
src/main.rs       clap CLI (symbol-rs)
```
