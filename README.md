# ca-tools

Codebase audit toolkit for Python projects.

Point at a directory, get the full picture — what frameworks it uses, where the entry points are, which files are dead, what executes on import, and how everything is wired together.

## Install

```bash
pip install ca-tools
```

## Commands

### `ca audit <path>` — Full codebase audit

Detect stack, entry points, orphan files, side effects, config files, unused deps.

```bash
ca audit /path/to/project
ca -v audit /path/to/project   # verbose — full detail
ca /path/to/project            # shortcut — defaults to audit
```

### `ca loc <path>` — Lines of code

GitHub Linguist-powered language detection with colored bar chart.

```bash
ca loc /path/to/project
```

### `ca map <path>` — Import graph analysis

Find circular imports, hotspots, fragile modules, deep chains, leaf modules.

```bash
ca map /path/to/project
ca map /path/to/project --blast src/models.py      # blast radius
ca map /path/to/project --min-chain 3               # show shorter chains
ca map /path/to/project --min-fan-in 3              # lower hotspot threshold
```

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

Add `[tool.ca-tools]` to the target project's `pyproject.toml`:

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

## Global options

```
-v, --verbose          Show full detail instead of compact output
--format json          Output as JSON (for CI/CD pipelines)
-i, --include PATTERN  Only analyze files matching glob pattern
-e, --exclude PATTERN  Skip files matching glob pattern
```

## Why

Most Python audit tools find problems *inside* files (lint, types, dead code). ca-tools finds problems *between* files — the architecture-level view:

- **knip** does this for JavaScript/TypeScript. Python doesn't have an equivalent. ca-tools fills that gap.
- **GitHub Linguist** ported to Python — accurate language detection with 500+ languages, multi-strategy detection (modeline, shebang, filename, extension), real GitHub colors.
- **scc-style** LOC counting with language breakdown and GitHub-style colored bar.
- **Import graph analysis** — circular imports, hotspots, blast radius. Things no other Python tool shows.
- First thing you run on an unfamiliar codebase, before reading a single line of code.
- Static analysis only — never imports or executes the target code.

## Architecture

```
src/ca_tools/
├── cli.py                  Typer root CLI
├── shared/                 Shared utilities
│   ├── files.py            File collection with include/exclude
│   ├── findings.py         Severity system (error/warning/info)
│   ├── project_config.py   [tool.ca-tools] config loader
│   └── spec.py             Detection spec loader
├── data/
│   └── spec.toml           Community-editable detection patterns
├── audit/                  ca audit
│   ├── cli.py
│   ├── stack.py            Stack detection from deps
│   ├── entrypoints.py      Entry point detection
│   ├── orphans.py          Import graph + orphans
│   ├── sideeffects.py      Side effect detection
│   ├── config.py           Config file detection
│   ├── unused_deps.py      Unused dep detection
│   └── registry.py         Package name lookup
├── loc/                    ca loc
│   ├── cli.py
│   └── linguist/           GitHub Linguist port
│       ├── linguist.py     Detection engine
│       ├── language.py     Language registry (500+ langs)
│       ├── blob.py         File abstraction
│       ├── strategy/       Detection strategies
│       └── config/         YAML language definitions
├── map/                    ca map
│   ├── cli.py
│   └── analyzer.py         Cycles, hotspots, blast radius
└── init/                   ca init
    └── cli.py              Config generator
```

## License

MIT
