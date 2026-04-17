# symbol

AST-native codebase audit, symbol index, and MCP server for Python projects.

Point at a directory, get the full picture — what frameworks it uses, where the entry points are, which files are dead, what executes on import, where the TODOs and swallowed exceptions hide, and how everything is wired together.

## Install

```bash
pip install symbol
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

GitHub Linguist-powered language detection with colored bar chart. 500+ languages, multi-strategy detection (modeline, shebang, filename, extension, XML, manpage).

```bash
symbol loc /path/to/project
```

### `symbol map <path>` — Import graph analysis

Find circular imports, hotspots, fragile modules, deep chains, leaf modules, blast radius.

```bash
symbol map /path/to/project
symbol map /path/to/project --blast src/models.py      # blast radius
symbol map /path/to/project --min-chain 3              # show shorter chains
symbol map /path/to/project --min-fan-in 3             # lower hotspot threshold
```

### `symbol analyze <file>` / `symbol dump <path>` — Per-file AST analysis

Inspect imports, definitions, and call sites for one file, or dump the full parsed graph.

### `symbol search <pattern>` — Find symbols by name

Columnar symbol index over the whole project. Multiple patterns are AND-ed. Returns signatures + locations, no bodies.

```bash
symbol search UserService                  # exact or suffix match on qualified path
symbol search user service --fixed         # all patterns must appear as substrings
symbol search '^get_' --regex              # Python regex
symbol search save --kind method
```

### `symbol code <address>` — Fetch exact body

Retrieve a symbol's body by qualified path or `file:start-end` range. Also populates the read cache consumed by `symbol patch`.

```bash
symbol code services.user.UserService         # by symbol path
symbol code services.user.UserService.save    # method
symbol code src/services/user.py:120-145      # by explicit line range
```

### `symbol outline <file>` — Symbol tree of a file

Parent-child tree of classes, functions, methods in one file.

### `symbol callers <name>` — Tier-1 textual reference scan

Find plausible call sites for a name. Textual match; may include false positives. Use `symbol code` to verify each hit.

### `symbol patch <file>` — Byte-range edit

Edit an existing file by line range. Replace (with content), delete (empty content), or insert (zero-width range). Token-efficient alternative to raw `Edit` for agents: no `old_string` payload.

```bash
symbol patch src/foo.py --range 10-20 --content 'new body'    # replace
symbol patch src/foo.py --range 10-20 --content ''            # delete
symbol patch src/foo.py --range 10-10 --content 'import os'   # insert before line 10

symbol patch src/foo.py --range 10-20 --content '...' --dry-run   # preview diff
symbol patch src/foo.py --range 10-20 --content '...' --force     # skip read-cache check
symbol patch src/foo.py --range 10-20 --content '...' --agent     # plain text for LLMs
symbol patch src/foo.py --range 10-20 --content '...' --format json
```

Exit codes: `0` applied/dry-run, `1` error, `2` needs_read_confirmation.

### `symbol init <path>` — Generate config

Analyze a project and generate a recommended `[tool.symbol]` config.

```bash
symbol init /path/to/project
```

### `symbol update-linguist` — Update language definitions

Pull latest language definitions from GitHub's linguist repository.

```bash
symbol update-linguist
```

## Configuration

Two ways to configure `symbol` for a target project:

1. **`symbol.toml`** at the project root — standalone, works even if you don't own the project
2. **`[tool.symbol]`** in `pyproject.toml`

```toml
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

See `docs/spec-schema.md` for the full spec schema.

## Global options

```
-v, --verbose          Show full detail instead of compact output
--format json          Output as JSON (for CI/CD pipelines)
-i, --include PATTERN  Only analyze files matching glob pattern
-e, --exclude PATTERN  Skip files matching glob pattern
```

## Why

Most Python audit tools find problems *inside* files (lint, types, dead code). `symbol` finds problems *between* files — the architecture-level view:

- **knip** does this for JavaScript/TypeScript. Python didn't have an equivalent. `symbol` fills that gap.
- **GitHub Linguist** ported to Python — accurate language detection with 500+ languages and real GitHub colors.
- **scc-style** LOC counting with language breakdown and colored bar.
- **Import graph analysis** — circular imports, hotspots, blast radius. Things no other Python tool surfaces.
- First thing you run on an unfamiliar codebase, before reading a single line of code.
- Static analysis only — never imports or executes the target code.

## Architecture

```
src/ca/symbol/
├── cli.py                  Typer root CLI
├── commands/               Thin command views
│   ├── audit.py            runs all checkers
│   ├── loc.py              language stats
│   ├── map.py              import graph
│   ├── analyze.py          per-file / dump
│   └── init.py             config generator
├── checkers/               @register'd checkers
│   ├── stack.py            tech stack from deps
│   ├── entrypoints.py      __main__ guards
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
│   └── linguist/           GitHub Linguist port (500+ langs)
└── data/
    ├── spec.toml           Global baseline spec
    └── specs/NAME/         Per-package specs (200+ packages:
                            django, fastapi, celery, sqlalchemy, ...)
```

## Contributing

Adding detection for a new package? Drop a spec in `src/ca/symbol/data/specs/NAME/spec.toml` — no Python changes required. See `CONTRIBUTING.md` and `docs/spec-schema.md`.

## License

MIT
