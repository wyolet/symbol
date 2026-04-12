# ca-tools

Codebase audit toolkit for Python projects. Point at a directory, get the full picture.

## What it does

1. **`ca audit`** — Stack detection, entry points, orphan files, side effects, config files, unused deps, import graph health
2. **`ca loc`** — GitHub Linguist-powered LOC counter with 500+ language detection and colored bar chart
3. **`ca map`** — Import graph analysis: circular imports, hotspots, fragile modules, deep chains, blast radius
4. **`ca init`** — Generate recommended `[tool.ca-tools]` config from project analysis
5. **`ca update-linguist`** — Pull latest language definitions from GitHub

## Structure

```
src/ca_tools/
├── cli.py              — Typer root CLI with subcommands
├── shared/             — Shared utilities (files, findings, config, spec)
├── data/spec.toml      — Community-editable detection patterns
├── audit/              — Codebase audit tool
├── loc/                — LOC counter + linguist port
│   └── linguist/       — GitHub Linguist port (500+ langs, multi-strategy)
├── map/                — Import graph analysis
└── init/               — Config generator
```

## Conventions

- Python 3.11+ (uses tomllib from stdlib). NO `from __future__ import annotations`.
- CLI framework: Typer (includes Click + Rich)
- Package manager: uv (use `uv run`, `uv add`, not pip)
- All analysis uses stdlib `ast` module — no runtime execution, pure static analysis
- Output should be clean, scannable, categorized with Rich formatting
- Compact output by default, `-v` for full detail
- `--format json` for CI integration
- Test against scarlet/api as primary test target
- CLI entry point: `ca <path>` runs full audit

## Detection spec

The curated `spec.toml` maps packages to categories, defines config file patterns, side effect lists, and entrypoint patterns. Community can edit this file without touching Python code.

## Project config

Target projects configure ca-tools via `[tool.ca-tools]` in their pyproject.toml:
- `include`/`exclude` glob patterns
- Per-section severity overrides (error/warning/info)
- Per-section ignore lists

## Design principles

- Static analysis only — never import or execute the target code
- Minimal false positives over completeness
- One command per concern — audit, loc, map are separate tools
- Output should be useful in the first 30 seconds of encountering an unfamiliar codebase
- CI-friendly: exit code 1 on errors, `--format json` for machine consumption
