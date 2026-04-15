# ca-tools

Codebase audit toolkit for Python projects. Point at a directory, get the full picture.

## Commands

- **`ca audit`** — Runs all registered checkers: stack, entrypoints, orphans, side effects, swallowed exceptions, TODOs, unused deps, code structure
- **`ca loc`** — GitHub Linguist-powered LOC counter (500+ languages, multi-strategy detection)
- **`ca map`** — Import graph: circular imports, hotspots, fragile modules, deep chains, blast radius
- **`ca analyze` / `ca dump`** — Per-file AST analysis
- **`ca init`** — Generate recommended `[tool.ca-tools]` config
- **`ca update-linguist`** — Pull latest language definitions from GitHub

## Structure

```
src/ca_tools/
├── cli.py                — Typer root (dispatches, defaults bare-path to audit)
├── commands/             — audit, loc, map, analyze, init (thin views)
├── checkers/             — @register'd checkers (file-kind and project-kind)
│   ├── stack.py, entrypoints.py, orphans.py, side_effects.py,
│   ├── swallowed.py, todos.py, unused_deps.py, code_structure.py
├── shared/               — AnalysisContext, ASTCache, registry, runner,
│   ├── spec, config_resolver, framework_detector, graph, linguist/
└── data/
    ├── spec.toml         — Global baseline spec
    └── specs/NAME/       — Per-package specs (200+ packages)
```

## Architecture

- **Checker registry** (`shared/registry.py`) — `@register(name, kind, ...)` + `views(name, rich=, json=, findings=)`. `kind="file"` runs per file; `kind="project"` runs once. Commands are thin views, not owners.
- **AnalysisContext** (`shared/context.py`) — built once via `build_context()`: project_root, spec, config, ASTCache, frameworks, deps, resolved config. Shared across audit/map/analyze.
- **ASTCache** (`shared/ast_cache.py`) — parses each file once; passed to all consumers.
- **Spec system** (`shared/spec.py`, `shared/config_resolver.py`):
  1. Global baseline (`data/spec.toml`)
  2. Per-package specs (`data/specs/NAME/spec.toml`) — loaded only if package appears in project deps (stdlib always loaded)
  3. Project config (`ca-tools.toml` at root, or `[tool.ca-tools]` in pyproject.toml)
- **Package spec namespaces**: `[checkers.orphan]`, `[checkers.side_effects.calls]`, `[checkers.side_effects.patterns]`, `[checker]` (AST exclude), `[scanner]` (LOC exclude)
- **Pipeline hooks** (`shared/pipeline.py`) — `@hook(pipeline, priority)` for `DEPS`, `SKIP_ORPHAN`, `ENTRYPOINTS`, `IMPORTS`. Framework-specific logic lives in package specs, not core checkers.

## Conventions

- Python 3.11+ (stdlib `tomllib`). **Never** `from __future__ import annotations`.
- CLI: Typer. Package manager: uv.
- Static analysis only — never import or execute target code.
- Terminology: **checker** (matches ruff/pylint), not "detector".
- Package-specific filenames (admin.py, urls.py) belong in package specs, never in global spec. Global is Python/community conventions only (app.py, main.py, conftest.py, etc.).
- `__init__.py` re-exports are conventions — not cycles, not hotspots.
- Output: compact by default, `-v` for detail, `--format json` for CI.

## Survey corpus

`survey/repos/` (gitignored) — 25–35 cloned repos for false-positive regression testing. Each can carry its own `ca-tools.toml` for project-level ignores.

## Design principles

- Static analysis only
- Minimal false positives over completeness
- One command per concern
- Useful within 30 seconds of encountering an unfamiliar codebase
- CI-friendly: exit 1 on errors, JSON for machines
