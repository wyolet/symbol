# symbol

**AST-native code intelligence for Python.** A CLI for humans, an MCP server for agents.

Point `symbol` at a directory and ask it questions a human or a coding agent actually has:

- What frameworks does this project use, where are the entry points, which files are dead, what runs on import, where the TODOs and swallowed exceptions hide?
- Where is `UserService` defined, who calls `db.commit`, what's the body of `process_payment` — without re-reading 800 lines of file?

Static analysis only — `symbol` never imports or executes the target code.

> **Status:** v0.1.0, install from GitHub (no PyPI yet). Python is the proving ground. **Go** and **TypeScript** are next on the roadmap.

## Install

### CLI only

```bash
uv tool install --from git+https://github.com/wyolet/symbol@v0.1.0 symbol
```

Then `symbol audit /path/to/project`, `symbol loc`, `symbol map` work from any directory.

### Claude Code plugin (CLI + MCP server + skill + hooks)

For full agent integration — MCP tools (`SearchSymbol`, `SymbolBody`, `MultiPatch`, …), the `symbol` skill, and soft-nudge PreToolUse / PostToolUse hooks that steer Claude away from native Grep/Read/Edit on indexed Python files:

```bash
# 1. Install the CLI (the plugin's MCP server and hooks call it)
uv tool install --from git+https://github.com/wyolet/symbol@v0.1.0 symbol

# 2. Install the plugin in Claude Code
claude plugin install git+https://github.com/wyolet/symbol@v0.1.0
```

### Other agent coding tools

The MCP server is plain stdio MCP — it works anywhere MCP works. **opencode**, Cursor, Continue, and Zed integrations are tracked in [#8](https://github.com/wyolet/symbol/issues/8); help wanted. The Claude-Code-specific glue (skill + hooks) does not port automatically.

### Updating / uninstall

```bash
uv tool upgrade symbol           # or: uv tool uninstall symbol
claude plugin update symbol      # or: claude plugin uninstall symbol
```

## Commands

### `symbol audit <path>` — Full codebase audit

Runs all registered checkers: stack detection, entry points, orphan files, side effects, swallowed exceptions, TODOs, unused deps, code structure metrics.

```bash
symbol audit /path/to/project
symbol -v audit /path/to/project   # verbose — full detail
symbol /path/to/project            # shortcut — defaults to audit
```

### `symbol loc <path>` — Lines of code

GitHub Linguist port: 500+ languages, real GitHub colors, multi-strategy detection (modeline, shebang, filename, extension, XML, manpage). Colored bar chart by default.

```bash
symbol loc /path/to/project
```

### `symbol map <path>` — Import graph analysis

Circular imports, hotspots, fragile modules, deep chains, leaf modules, blast radius.

```bash
symbol map /path/to/project
symbol map /path/to/project --blast src/models.py      # blast radius
symbol map /path/to/project --min-chain 3              # show shorter chains
symbol map /path/to/project --min-fan-in 3             # lower hotspot threshold
```

### Symbol-level inspection

```bash
symbol search UserService              # exact / suffix match on qualified path
symbol search user service --fixed     # all patterns must appear as substrings
symbol search '^get_' --regex          # Python regex
symbol search save --kind method

symbol code services.user.UserService         # body by qualified path
symbol code services.user.UserService.save    # method
symbol code src/services/user.py:120-145      # by explicit line range

symbol outline src/services/user.py    # parent-child tree of one file
symbol callers UserService             # textual tier-1 reference scan
```

### `symbol patch <file>` — Byte-range edit

Edit by line range without sending an `old_string` payload. Replace (with content), delete (empty content), or insert (zero-width range).

```bash
symbol patch src/foo.py --range 10-20 --content 'new body'    # replace
symbol patch src/foo.py --range 10-20 --content ''            # delete
symbol patch src/foo.py --range 10-10 --content 'import os'   # insert before line 10

symbol patch src/foo.py --range 10-20 --content '...' --dry-run   # preview diff
symbol patch src/foo.py --range 10-20 --content '...' --force     # skip read-cache check
symbol patch src/foo.py --range 10-20 --content '...' --agent     # plain text for LLMs
```

Exit codes: `0` applied/dry-run, `1` error, `2` needs_read_confirmation.

### Plus

`symbol analyze <file>`, `symbol dump <path>`, `symbol init <path>`, `symbol update-linguist`, `symbol undo`, `symbol refresh [--full]`.

## MCP surface (12 agent tools)

When run as `symbol mcp` (or installed via the plugin), `symbol` exposes:

| Read | Write | Safety |
| --- | --- | --- |
| `SearchSymbol` | `Patch` | `Undo` |
| `SymbolBody` | `MultiPatch` | `Refresh` |
| `SymbolOutline` | `InsertSymbol` | |
| `SymbolCallers` | `DeleteSymbol` | |
| | `RenameSymbol` | |
| | `ReplaceSymbol` | |

`Undo` is transactional and operates on `.symbol/transactions/` — no git involvement, no staged changes touched. `Refresh` is the escape hatch when the index drifts.

## Configuration

Two equivalent forms — standalone or in `pyproject.toml`:

```toml
# symbol.toml at project root, or [tool.symbol] in pyproject.toml
[tool.symbol]
exclude = ["alembic/*", "scripts/*"]

[tool.symbol.severity]
orphans = "warning"        # default: error
side_effects = "info"      # default: warning
unused_deps = "error"      # default: error

[tool.symbol.ignore]
deps = ["greenlet", "psycopg"]
orphans = ["alembic/*", "src/main.py"]
side_effects = ["*.include_router()", "*.add_middleware()"]
```

See [`docs/spec-schema.md`](docs/spec-schema.md) for the full spec schema.

## Global options

```
-v, --verbose          Show full detail instead of compact output
--format json          Output as JSON (for CI/CD pipelines)
-i, --include PATTERN  Only analyze files matching glob pattern
-e, --exclude PATTERN  Skip files matching glob pattern
```

## Why

Most Python tools find problems *inside* files (lint, types, dead code). `symbol` finds problems *between* files — and exposes the result to agents in tokens, not line ranges:

- **knip** does this for JavaScript/TypeScript. Python didn't have an equivalent. `symbol` fills that gap.
- **GitHub Linguist ported to Python** — accurate detection for 500+ languages with real GitHub colors.
- **scc-style LOC** with language breakdown and colored bar chart.
- **Import-graph analysis** — circular imports, hotspots, blast radius. Things no other Python tool surfaces.
- **Agent-friendly write surface** — symbol-level patch / rename / replace with byte-range edits and a transactional undo log, so coding agents don't have to re-read entire files to make safe changes.

First thing you run on an unfamiliar codebase, before reading a single line of code.

## Architecture

```
src/wyolet/symbol/
├── cli.py                  Typer root CLI
├── commands/               Thin command views (audit, loc, map, analyze,
│                           search, code, outline, callers, patch, refresh,
│                           undo, init, + symbol-level ops for MCP)
├── checkers/               @register'd checkers
│   ├── stack.py            tech stack from deps
│   ├── entrypoints.py      __main__ guards, framework hooks
│   ├── orphans.py          unreachable files
│   ├── side_effects.py     bare module-level calls
│   ├── swallowed.py        silenced exceptions
│   ├── todos.py            TODO/FIXME/HACK/XXX
│   ├── unused_deps.py      declared but unimported
│   └── code_structure.py   functions, classes, type coverage
├── shared/                 Core infrastructure
│   ├── context.py          AnalysisContext (root, spec, cache, config)
│   ├── ast_cache.py        parse once, share across checkers
│   ├── registry.py         @register + views()
│   ├── runner.py           dispatches file/project checkers
│   ├── spec.py             spec loader
│   ├── config_resolver.py  spec → packages → project-config layering
│   ├── framework_detector.py
│   ├── pipeline.py         @hook(pipeline, priority)
│   ├── graph.py            import graph primitives
│   ├── symbol_index.py     qualified-path index for MCP read/write tools
│   └── linguist/           GitHub Linguist port (500+ langs)
└── data/
    ├── spec.toml           Global baseline spec
    └── specs/NAME/         Per-package specs (237 packages: django, fastapi,
                            celery, sqlalchemy, langchain, pydantic, ...)
```

## Contributing

The fastest ways to help:

- **Add a package spec** for a library we don't cover yet — no Python required, just TOML. See [#3](https://github.com/wyolet/symbol/issues/3) and [`CONTRIBUTING.md`](CONTRIBUTING.md).
- **Run `symbol` on your real Python project** and file false positives. See [#6](https://github.com/wyolet/symbol/issues/6).
- **Benchmark the MCP surface** against native Read/Grep/Edit on representative agent tasks. See [#7](https://github.com/wyolet/symbol/issues/7).
- **Wire `symbol mcp` into opencode / Cursor / Continue / Zed**. See [#8](https://github.com/wyolet/symbol/issues/8).

Pinned issues on the repo show what's most useful right now.

## License

MIT
