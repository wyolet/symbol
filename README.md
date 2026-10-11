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

> **Status.** The Rust binary (`rust/`) implements the symbol index, the read
> and write surface, `loc`, the MCP server and the Claude Code hook; it is
> differentially verified **byte-for-byte** against the Python implementation
> it replaced across real corpora (flask, gin, this repo). The Python package
> keeps the Python-specific analysis commands: `audit`, `analyze`, `dump`,
> `init`, `map` (the Python import graph) and `update-linguist`. The binary's
> own `map` works over the index for every indexed language.

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

`pyscan` is compiled into the binary. Its modules are stdlib-only and are
unpacked once into the user cache dir (`~/Library/Caches/symbol` on macOS,
`$XDG_CACHE_HOME/symbol` or `~/.cache/symbol` elsewhere). They then run on the
first Python >= 3.11 found as `python3` or `python` on `PATH`; set
`SYMBOL_PYTHON` to choose the interpreter. Parity with the Python CLI is
verified with both on the same Python minor version. An older interpreter
can't parse newer syntax, and files that fail to parse are skipped.

Daemon discovery: `SYMBOL_PYSCAN` / `SYMBOL_GOSCAN` override the full command.
Without an override, `go-scan` (or `go-scan-<os>-<arch>`) is taken from the
directory holding the `symbol` executable, then from a source checkout
(`SYMBOL_REPO`, else the one the binary was built from). If the project has
files in a language whose daemon can't start, `index`, `refresh`, symbol-level
writes, and reads that need a refresh exit non-zero
(`daemon_unavailable`, or `python_unavailable` naming the required version)
and leave the index and files untouched.

## Install

### PyPI

```bash
uv tool install wyolet-symbol    # or: pipx install wyolet-symbol
```

Wheels for macOS 13+ (arm64, x86_64), Linux (x86_64, arm64; glibc or musl),
and Windows (x86_64) bundle the Rust `symbol` binary and `go-scan`. The
package's `symbol` runs `audit`, `analyze`, `dump`, `init`, `map`, and
`update-linguist` in Python and every other command on the binary, with
`pyscan` on the interpreter the package is installed into. Other platforms
get a pure wheel without binaries.

The binary is the first of: `SYMBOL_BIN`, the one bundled in the wheel,
`rust/target/release/symbol` or `rust/target/debug/symbol` when running from
a source checkout, and a `symbol` on `PATH` that is not a Python entry point
(such as the standalone binary below). Without one, those commands exit 1
and name these options.

### Standalone binary

Each [GitHub release](https://github.com/wyolet/symbol/releases) has
`symbol-<version>-<target>.tar.gz` (`.zip` for Windows) for `darwin-arm64`,
`darwin-amd64`, `linux-amd64`, `linux-arm64`, and `windows-amd64`, plus
`SHA256SUMS`. The archive holds `symbol` and `go-scan`; keep them in the same
directory.

```bash
version=0.2.5 target=linux-amd64
base=https://github.com/wyolet/symbol/releases/download/v$version
curl -fsSLO "$base/symbol-$version-$target.tar.gz" -O "$base/SHA256SUMS"
grep " symbol-$version-$target.tar.gz" SHA256SUMS | sha256sum -c   # macOS: shasum -a 256 -c
tar -xzf "symbol-$version-$target.tar.gz" -C ~/.local/bin
```

Linux binaries are statically linked. On macOS, archives downloaded through a
browser are quarantined: `xattr -d com.apple.quarantine symbol go-scan`.

### From source

```bash
git clone https://github.com/wyolet/symbol
cd symbol/rust
cargo build --release          # → target/release/symbol
```

Put `target/release` on your `PATH`, or alias it while building from source:

```bash
alias symbol="$PWD/target/release/symbol"
```

`make dist` builds the host platform's wheel and archive into `dist/` and
`build/archives/`.

The Go daemon (`go-scan`) is cross-compiled per platform in CI; a Go toolchain
lets you build it locally via `make build-go-scan`. For Go projects without
Python, place a `go-scan` binary next to `symbol`. Go renames, `refs`, and
`implementers` also need the Go toolchain (`go`) on `PATH`, since go-scan loads
packages through `packages.Load` for them; indexing and other reads don't. Python analysis needs only a
Python >= 3.11 interpreter: no pip install and no repo checkout.

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
symbol refs store.Store.Save              # type-resolved references (Go)
symbol implementers net/http.Handler      # implementers of an interface, or interfaces a type implements (Go)
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

### Import graph

The binary's `map` works over the index. The PyPI package's `symbol map PATH`
is the Python import graph instead (`symbol map --help`).

```bash
symbol map                                  # cycles, hotspots, fan-out, deep chains
symbol map -v                               # plus every import site and coverage detail
symbol map --exclude '*_test.go'            # leave files out of the graph
symbol map --dependents pkg/gateway/client  # everything that imports it, by depth
symbol map --deps cmd/server --depth 2      # what it imports, two hops out
```

Nodes are modules — a Go package, a Python module — derived from the
qualified paths in the index; edges are imports whose source names a module in
the project, including across Go modules in one tree. A module can be named in
full, by an indexed file path, or by a unique trailing part of its name. Cycle
edges list their import sites, so cycles that exist only through test files are
visible as such.

The index records each file's module only through its symbols and keeps
Python import sources as written. A file with no symbols (such as a
re-export-only `__init__.py`) has no node, and relative imports are not
resolved; `map` reports both as notes rather than counting them as edges.

### Lines of code

```bash
symbol loc                       # colored breakdown by language
symbol loc path/ --files         # per-file detail
symbol loc --classify src/app.py # show how each file was classified
```

### MCP server

```bash
symbol --root path/to/project mcp   # stdio MCP server for coding agents
```

| Tool | Does |
| --- | --- |
| `SearchSymbol`, `SymbolOutline`, `SymbolBody` | locate declarations, outline a file, fetch a body or line range |
| `SymbolCallers` | textual references by name (any language) |
| `SymbolRefs`, `SymbolImplementers` | type-resolved references and implements relation (Go) |
| `Patch`, `MultiPatch` | byte-range edits, read-cache gated |
| `ReplaceSymbol`, `InsertSymbol`, `DeleteSymbol`, `RenameSymbol` | symbol-level writes |
| `Undo`, `Refresh` | revert the last transaction, reindex |

Tool descriptions live in
[`src/wyolet/symbol/data/mcp_tools.toml`](src/wyolet/symbol/data/mcp_tools.toml).

## Configuration

`symbol.toml` at the project root, or the same tables under `[tool.symbol]` in
`pyproject.toml`. The Rust index honors global and project `[checker]` excludes:

```toml
[checker]
exclude = ["alembic/*", "scripts/*"]
```

`loc` takes `--exclude`, `--no-vendor-skip`, and `--config-dir` for one-off
overrides.

## Repository layout

```
rust/
├── symbol/     symbol-core → the `symbol` binary (index, reads, writes, loc)
├── linguist/   symbol-linguist → GitHub-Linguist port + LOC counting
├── dataset/    differential-test dataset mining and verification
└── harness/    black-box feature harness
```

`src/wyolet/symbol/` is the Python package: the `symbol` console entry, the
audit engine (checkers, spec system, import graph), the package specs, and
the `pyscan` daemon the binary embeds.

## License

MIT — see [`LICENSE`](LICENSE).
