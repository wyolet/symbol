<div align="center">

# symbol

**AST-native code intelligence, in Rust.**

A fast, single-binary code-intelligence engine for humans and coding agents.
Point it at a codebase and ask where a symbol is defined, who calls it, or
edit it precisely — without re-reading whole files.

![status](https://img.shields.io/badge/status-active%20rewrite-orange)
![license](https://img.shields.io/badge/license-MIT-blue)
![languages](https://img.shields.io/badge/languages-Python%20%7C%20Go-informational)

</div>

---

`symbol` answers the questions a human or an agent actually has about an
unfamiliar codebase:

- **Where** is `UserService` defined, **who** calls `db.commit`, **what** is the
  body of `process_payment` — by qualified path, in tokens, not line ranges.
- **Edit** a symbol safely: rename it and its references, replace its
  definition, insert next to it, or patch an exact byte range — every write
  transactional and reversible with `undo`.
- **Measure** a tree: lines of code across 500+ languages via a GitHub-Linguist
  port.

Static analysis only — `symbol` never imports or executes the target code.

> **Status.** This is the Rust rewrite (`symbol-rs`), the future of the
> project. Its core — the symbol index, the read surface, the write surface,
> and `loc` — is differentially verified **byte-for-byte** against the mature
> Python implementation across real corpora (flask, gin, this repo). The Python
> version is being wound down as the Rust surface reaches parity. `audit`, `map`
> (import graph), and the MCP server still live in Python today and will be
> ported to Rust after the core lands — nothing is being dropped.

## How it works

The engine is language-neutral. It never parses source in-process. Instead,
language analysis runs out-of-process in native-AST **daemons** that speak
newline-delimited JSON-RPC 2.0 over stdio ([`schemas/symbol.rpc.schema.json`](schemas/symbol.rpc.schema.json)):

| Language | Daemon | Backed by |
| --- | --- | --- |
| Python | `pyscan` | Python's own `ast` |
| Go | `go-scan` | `go/types` |

The Rust core owns everything neutral — the on-disk index, staleness and
incremental refresh, search/read/write orchestration, transactions, the read
cache, and LOC. A new language is a new daemon, not new branches in the engine.

Daemon discovery is by environment: `SYMBOL_PYSCAN` / `SYMBOL_GOSCAN` override
the full command; `SYMBOL_REPO` points at a checkout that provides both.

## Install

> Distribution for the Rust binary is not finalized yet. For now, build from
> source; a packaged release (`symbol` on your `PATH`) will land here.

```bash
git clone https://github.com/wyolet/symbol
cd symbol/rust
cargo build --release          # → target/release/symbol-rs
```

The built binary is `symbol-rs`; it takes the canonical `symbol` name once
packaged. Alias it while building from source:

```bash
alias symbol="$PWD/target/release/symbol-rs"
```

The Go daemon (`go-scan`) is cross-compiled per platform in CI; a Go toolchain
lets you build it locally via `make build-go-scan`. Python analysis uses the
`pyscan` adapter from this repo — no separate build.

## Usage

All commands take a global `--root PATH` (default `.`) and `--json` for machine
output.

### Index

```bash
symbol index          # build .symbol/symbol_index.msgpack.zst
symbol refresh        # reindex only changed files
symbol stats          # index statistics
```

The index builds lazily — read and write commands build or refresh it as
needed.

### Read

```bash
symbol search UserService                 # narrow candidates by name (signatures only)
symbol search user service --fixed        # every pattern must appear as a substring
symbol search '^get_' --regex             # regex match
symbol search save --kind method --limit 20

symbol code services.user.UserService     # body by qualified path
symbol code services.user.UserService.save
symbol code src/services/user.py:120-145  # body by explicit line range

symbol outline src/services/user.py       # parent-child symbol tree of a file
symbol callers UserService                # tier-1 textual reference scan
```

### Write

Every write is transactional and reversible. Pass `--dry-run` to preview the
diff without touching disk.

```bash
# Byte-range edit — replace, or delete with empty content
symbol patch src/foo.py 10-20 'new body'
symbol patch src/foo.py 10-20 ''

# Multiple non-overlapping edits to one file, applied atomically
symbol multi-patch src/foo.py '[{"range":"10-12","content":"..."},{"old":"x=1","content":"x=2"}]'

# Symbol-level ops by qualified path
symbol rename-symbol services.user.UserService AccountService   # renames refs too
symbol replace-symbol services.user.UserService.save 'def save(self): ...'
symbol insert-symbol services.user.UserService after 'def new_method(self): ...'
symbol delete-symbol services.user.old_helper                   # refuses if callers exist; --force overrides

symbol undo           # revert the most recent transaction
```

`insert-symbol` positions are `before` / `after` / `start` / `end` (relative to
the anchor symbol). Range edits run a read-cache preflight so an agent can't
patch a stale view of a file; `--force` skips it.

### Lines of code

```bash
symbol loc                       # colored breakdown by language
symbol loc path/ --files         # per-file detail
symbol loc --classify src/app.py # show how each file was classified
```

## Configuration

`symbol.toml` at the project root, or the same tables under `[tool.symbol]` in
`pyproject.toml`. The Rust index honors global and project `[checker]` excludes:

```toml
[checker]
exclude = ["alembic/*", "scripts/*"]
```

`loc` takes `--exclude`, `--no-vendor-skip`, and `--config-dir` for one-off
overrides.

## Interop with the Python version

During the transition, both implementations operate on the same `.symbol/`
state, so a mixed agent session is safe:

- **Index** — `.symbol/symbol_index.msgpack.zst` is byte-compatible (v8). Either
  implementation loads the other's index.
- **Transactions** — `symbol undo` in one implementation rolls back writes
  committed by the other.
- **Read cache** — shared session cache, so patch preflight works across both.

## Repository layout

```
rust/
├── symbol/     symbol-core → the `symbol-rs` binary (index, reads, writes, loc)
├── linguist/   symbol-linguist → GitHub-Linguist port + LOC counting
├── dataset/    differential-test dataset mining and verification
└── harness/    black-box feature harness
```

The Python implementation (`src/wyolet/symbol/`) remains in the tree until the
Rust surface reaches parity.

## License

MIT — see [`LICENSE`](LICENSE).
