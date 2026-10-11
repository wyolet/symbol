# symbol-core (Rust)

Rust port of symbol's language-neutral core: the symbol index, the read
surface (search / code / outline / callers), and the write surface (patch /
multi-patch / delete-symbol / insert-symbol / replace-symbol / rename-symbol /
undo), plus
LOC counting, behind the `symbol` binary. Language analysis never happens
in-process — it runs in native-AST daemons speaking newline-delimited JSON-RPC
2.0 over stdio (`schemas/symbol.rpc.schema.json`):

- **Python** → `wyolet.symbol.adapters.pyscan` (new; ~150-line stdio socket
  around the existing `PythonAstAdapter` — Python code is analyzed by
  Python's own `ast`, exactly as before)
- **Go** → the `daemon/go-scan` binary

Daemon discovery: `SYMBOL_PYSCAN` / `SYMBOL_GOSCAN` (full command override).
go-scan is otherwise looked up in order: `go-scan` or `go-scan-<os>-<arch>`
next to the running executable (symlinks resolved), the dev builds under
`SYMBOL_REPO` (else the crate's own repo), then that checkout's
wheel-bundled `src/wyolet/symbol/bin/go-scan-<os>-<arch>`.

Go renames also need the Go toolchain (`go`) on `PATH`: go-scan's rename
loads the project with `packages.Load`, which shells out to `go`. Indexing
and reads only parse files and work without it.

A daemon that can't start is an error, not a filter. Python's registry
reports such a language as unsupported and indexes around it; the Rust
index build and refresh instead stop with `daemon_unavailable` (language,
daemon, env var) before saving, so an existing index is never replaced by
one missing that language. Symbol-level writes that need the daemon
refuse with the same error before touching files. Reads on an index that needs no refresh still fall
back to first-line signatures and previews.

## Interop guarantees (verified end-to-end)

- **Index**: `.symbol/symbol_index.msgpack.zst` is byte-compatible v8 —
  Python loads Rust-built indexes and vice versa. (Gotcha encoded in
  `index.rs`: Python's `zstandard.decompress()` requires the frame content
  size, so saves use `zstd::bulk::compress`.)
- **Transactions**: `.symbol/transactions/<id>/manifest.json` matches
  Python's format — Python `symbol undo` rolls back Rust-committed writes
  and Rust `symbol undo` rolls back Python's.
- **Read cache**: `.symbol/cache/sessions/<SYMBOL_SESSION_ID>.json`, same
  JSON shape as caches/disk.py, so patch preflight works across both CLIs
  in one agent session.

## Current scope boundary

- **rename-symbol**: supported Python and Go kinds use the same v2
  receiver-aware daemon analysis as the Python CLI. Unsupported kinds retain
  the shared tier-1 fallback. Results carry `"engine": "v2"` or
  `"engine": "tier1"`.
- **Parameter renames** are not a standalone verb. `replace-symbol` detects
  unambiguous signature parameter edits and propagates Python keyword call
  sites in the same transaction.
- **multi-patch** atomically applies multiple non-overlapping range-addressed
  or exact-old edits to one file. Range edits use the same read-cache preflight
  as `patch`; exact-old edits must match once and prove their own context.
- **audit / map / analyze / MCP server not ported** — out of scope by
  design; they're the Python-specific engine documented in CLAUDE.md.
- Index build applies linguist classification (heuristics off = Python
  chain), the adapter gate, and global/project `[checker]` excludes. It does
  not yet load per-package spec excludes.
- Blank-gap normalization after delete/replace goes through the daemon
  (`normalize_blank_gaps` RPC), preserving the PEP 8 behavior for Python;
  other languages skip it.

## Layout

```
src/rpc.rs        JSON-RPC stdio client (go_ast transport, reused)
src/adapters.rs   registry + daemon adapters + wire types (protocols/types.py)
src/index.rs      shared/symbol_index.py — v8 format, staleness, refresh
src/reads.rs      reads/ — search, code, outline, callers
src/writes/       writes/ — patch, tx+undo, read_cache, content, ops, rename
src/main.rs       clap CLI (the `symbol` binary)
```

## Differential verification

Full differential sweep via `scripts/diff_runner.py` with the authored
datasets in `scripts/datasets/` (every target derived from real index
dumps). Ground truth is the Python implementation; verdicts count every
assertion (per-qpath index rows, per-op canonical diffs, undo round-trips).
Reports: `scripts/datasets/reports/`.

Corpora: flask `36e4a824` (pallets/flask), gin `34dac209` (gin-gonic/gin),
self = this repo at `5f82bcce` (`git archive` copy), fixture-proj
`e38a36b7` (smoke files).

| corpus | index assertions | dataset ops | match | both_refused | known_divergence | MISMATCH |
|---|---|---|---|---|---|---|
| flask | 1787 / 1787 | 425 (316r/109w) | 588 | 1 | 0 | **0** |
| gin | 1614 / 1614 | 383 (290r/93w) | 432 | 37 | 0 | **0** |
| self | 2284 / 2284 | 384 (319r/65w) | 494 | 0 | 0 | **0** |
| fixture-proj (smoke ×2) | 16 / 16 | 3 + 9 | 20 | 0 | 0 | 0 |

`both_refused` writes agree on the refusal but exercise no write path. On
gin they are build-tag duplicates (`symbol_ambiguous`), `needs_review`, and
symbols defined in `_test.go` files (`nothing_to_rename` — go-scan loads
packages without tests).

Index diff is **100% byte-equivalent on all four corpora** (v8 rows incl.
field/constant kinds, class bases, refs, byte ranges, imports). Every
dataset op — reads (code/callers/outline/search incl. Go signatures and
limit-truncated searches) and writes (197 renames incl. Go
interface-implicating + receiver-discriminated, Python MRO/inheritance,
field renames with same-name decoys, constants, and parameter edits through
replace-symbol; insert-symbol all four positions; patches; deletes; every
expect_error refusal) — agrees byte-for-byte, with clean undo round-trips on
transaction-backed ops. `known_divergence` is 0 everywhere; the
`engine-kind` tag never fired.

The initial sweep surfaced 229 mismatches which root-caused to SEVEN
defects — none in the tier-2 program's feature work; all in the
pre-program Rust core, the harness plumbing, or the Python oracle itself.
All fixed and re-swept to zero:

1. Go `signature` wire-key mismatch (`text` vs go-scan's `source`).
2. Missing Go client-side signature fallback (first non-blank line when
   the daemon can't parse a slice) — then re-scoped go-only after the
   first fix leaked it into Python field/constant signatures.
3. `search` limit-truncation ordering: candidates now iterate in
   first-seen row order (Python dict semantics), not alphabetically.
4. Runner: `patch` and `replace-symbol` Python CLI invocations used
   positional args where the CLI takes options.
5. Runner: undo-restores-pristine was asserted for non-transactional ops.
6. Cross-implementation mtime rounding: CPython computes st_mtime as
   `sec + nsec*1e-9`; the Rust `(sec*1e9+nsec)/1e9` differed in the last
   ulp, making every Python-written snapshot look stale (spurious
   refreshes).
7. **Oracle bug (both implementations fixed): the incremental-refresh
   path applied no vendor filter** — full builds prune `testdata/` etc.,
   refresh smuggled them back, so index content depended on which path
   built it.

Timing medians per op (seconds; write ops are fair CLI-vs-CLI comparisons;
read ops are not — Python reads run in-process on a pre-loaded index while
the Rust binary pays full process startup per invocation):

| op | flask py / rs | gin py / rs | self py / rs |
|---|---|---|---|
| index build | 0.72 / 0.31 | 0.49 / 0.11 | 1.36 / 0.50 |
| rename-symbol | 0.81 / 0.41 | 0.97 / 0.58 | 1.41 / 0.60 |
| replace-symbol | 0.39 / 0.37 | 0.36 / 0.12 | 0.37 / 0.51 |
| insert-symbol | 0.74 / 0.34 | 0.51 / 0.15 | 1.37 / 0.56 |
| delete-symbol | 0.75 / 0.37 | 0.51 / 0.15 | 1.38 / 0.53 |
| patch | 0.35 / 0.01 | 0.33 / 0.01 | 0.34 / 0.02 |
| code (read) | 0.0003 / 0.062 | 0.0003 / 0.009 | 0.0003 / 0.062 |
| search (read) | 0.0006 / 0.061 | 0.0006 / 0.009 | 0.0007 / 0.062 |

Rust writes run ~1.7–2.6× faster end-to-end than the Python CLI on the
same ops; the Rust index build is 2.3–4.5× faster.
