# ca-tools

Codebase audit toolkit for Python projects.

Point at a directory, get the full picture — what frameworks it uses, where the entry points are, which files are dead, what executes on import, where the TODOs and swallowed exceptions hide, and how everything is wired together.

## Install

```bash
pip install ca-tools
```

## Commands

### `ca audit <path>` — Full codebase audit

Runs all registered checkers: stack detection, entry points, orphan files, side effects, swallowed exceptions, TODOs, unused deps, code structure metrics.

```bash
ca audit /path/to/project
ca -v audit /path/to/project   # verbose — full detail
ca /path/to/project            # shortcut — defaults to audit
```

### `ca loc <path>` — Lines of code

GitHub Linguist-powered language detection with colored bar chart. 500+ languages, multi-strategy detection (modeline, shebang, filename, extension, XML, manpage).

```bash
ca loc /path/to/project
```

### `ca map <path>` — Import graph analysis

Find circular imports, hotspots, fragile modules, deep chains, leaf modules, blast radius.

```bash
ca map /path/to/project
ca map /path/to/project --blast src/models.py      # blast radius
ca map /path/to/project --min-chain 3              # show shorter chains
ca map /path/to/project --min-fan-in 3             # lower hotspot threshold
```

### `ca analyze <file>` / `ca dump <path>` — Per-file AST analysis

Inspect imports, definitions, and call sites for one file, or dump the full parsed graph.

### `ca init <path>` — Generate config

Analyze a project and generate a recommended `[tool.ca-tools]` config.

```bash
ca init /path/to/project
```

### `ca update-linguist` — Update language definitions

Pull latest language definitions from GitHub's linguist repository.

```bash
ca update-linguist
```

## Configuration

Two ways to configure ca-tools for a target project:

1. **`ca-tools.toml`** at the project root — standalone, works even if you don't own the project
2. **`[tool.ca-tools]`** in `pyproject.toml`

```toml
[tool.ca-tools]
exclude = ["alembic/*", "scripts/*"]

[tool.ca-tools.severity]
orphans = "warning"        # default: error
side_effects = "info"      # default: warning
unused_deps = "error"      # default: error

[tool.ca-tools.ignore]
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

Most Python audit tools find problems *inside* files (lint, types, dead code). ca-tools finds problems *between* files — the architecture-level view:

- **knip** does this for JavaScript/TypeScript. Python didn't have an equivalent. ca-tools fills that gap.
- **GitHub Linguist** ported to Python — accurate language detection with 500+ languages and real GitHub colors.
- **scc-style** LOC counting with language breakdown and colored bar.
- **Import graph analysis** — circular imports, hotspots, blast radius. Things no other Python tool surfaces.
- First thing you run on an unfamiliar codebase, before reading a single line of code.
- Static analysis only — never imports or executes the target code.

## Architecture

```
src/ca_tools/
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

Adding detection for a new package? Drop a spec in `src/ca_tools/data/specs/NAME/spec.toml` — no Python changes required. See `CONTRIBUTING.md` and `docs/spec-schema.md`.

## License

MIT
