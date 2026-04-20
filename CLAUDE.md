# symbol

AST-native codebase audit, symbol index, and MCP server for Python projects. Point at a directory, get the full picture.

Repo lives at [`github.com/wyolet/symbol`](https://github.com/wyolet/symbol) under the `wyolet` umbrella. Local path: `/Users/abror/projects/wyolet/symbol`. CLI and (eventual) PyPI distribution are both `symbol`.

## Commands

- **`symbol audit`** — Runs all registered checkers: stack, entrypoints, orphans, side effects, swallowed exceptions, TODOs, unused deps, code structure
- **`symbol loc`** — GitHub Linguist-powered LOC counter (500+ languages, multi-strategy detection)
- **`symbol map`** — Import graph: circular imports, hotspots, fragile modules, deep chains, blast radius
- **`symbol analyze` / `symbol dump`** — Per-file AST analysis
- **`symbol init`** — Generate recommended `[tool.symbol]` config
- **`symbol update-linguist`** — Pull latest language definitions from GitHub
- **`symbol mcp [--root PATH]`** — Run the MCP server (stdio) exposing 10 agent tools: SearchSymbol, SymbolBody, SymbolOutline, SymbolCallers, Patch, MultiPatch, DeleteSymbol, InsertSymbol, RenameSymbol, ReplaceSymbol

## MCP

`.mcp.json` at the repo root registers the server for Claude Code (project scope). `.claude/skills/symbol/SKILL.md` steers tool selection away from native Read/Grep/Edit. To install in another project, add this block to that project's `.mcp.json`:

```json
{"mcpServers": {"symbol": {"command": "uv", "args": ["run", "--directory", "/path/to/this-repo", "symbol", "mcp", "--root", "/path/to/target-project"]}}}
```

## Structure

```
src/wyolet/                    — namespace package (no __init__.py — PEP 420)
└── symbol/
    ├── cli.py                — Typer root (dispatches, defaults bare-path to audit)
    ├── commands/             — audit, loc, map, analyze, init, hook (thin views)
    ├── checkers/             — @register'd checkers (file-kind and project-kind)
    │   ├── stack.py, entrypoints.py, orphans.py, side_effects.py,
    │   ├── swallowed.py, todos.py, unused_deps.py, code_structure.py
    ├── shared/               — AnalysisContext, ASTCache, registry, runner,
    │   ├── spec, config_resolver, framework_detector, graph, linguist/
    └── data/
        ├── spec.toml         — Global baseline spec
        └── specs/NAME/       — Per-package specs (200+ packages)
```

Imports go `from wyolet.symbol.X import Y`. The PyPI distribution is `symbol`; future sibling packages (`linter`, etc.) install into the same `wyolet/` namespace.

## Architecture

- **Checker registry** (`shared/registry.py`) — `@register(name, kind, ...)` + `views(name, rich=, json=, findings=)`. `kind="file"` runs per file; `kind="project"` runs once. Commands are thin views, not owners.
- **AnalysisContext** (`shared/context.py`) — built once via `build_context()`: project_root, spec, config, ASTCache, frameworks, deps, resolved config. Shared across audit/map/analyze.
- **ASTCache** (`shared/ast_cache.py`) — parses each file once; passed to all consumers.
- **Spec system** (`shared/spec.py`, `shared/config_resolver.py`):
  1. Global baseline (`data/spec.toml`)
  2. Per-package specs (`data/specs/NAME/spec.toml`) — loaded only if package appears in project deps (stdlib always loaded)
  3. Project config (`symbol.toml` at root, or `[tool.symbol]` in pyproject.toml)
- **Package spec namespaces**: `[checkers.orphan]`, `[checkers.side_effects.calls]`, `[checkers.side_effects.patterns]`, `[checker]` (AST exclude), `[scanner]` (LOC exclude)
- **Pipeline hooks** (`shared/pipeline.py`) — `@hook(pipeline, priority)` for `DEPS`, `SKIP_ORPHAN`, `ENTRYPOINTS`, `IMPORTS`. Framework-specific logic lives in package specs, not core checkers.

## Conventions

- Python 3.13. **Never** `from __future__ import annotations` — use native annotations only.
- CLI: Typer. Package manager: uv.
- **Never hand-edit `pyproject.toml` for deps** — use `uv add`, `uv remove`, `uv sync --all-groups`.
- **No inline scripts** — save to `.tmp/<name>.py` and run `uv run .tmp/<name>.py`.
- Static analysis only — never import or execute target code.
- Terminology: **checker** (matches ruff/pylint), not "detector".
- Package-specific filenames (admin.py, urls.py) belong in package specs, never in global spec. Global is Python/community conventions only (app.py, main.py, conftest.py, etc.).
- `__init__.py` re-exports are conventions — not cycles, not hotspots.
- Output: compact by default, `-v` for detail, `--format json` for CI.

## Design principles

- Static analysis only
- Minimal false positives over completeness
- One command per concern
- Useful within 30 seconds of encountering an unfamiliar codebase
- CI-friendly: exit 1 on errors, JSON for machines
