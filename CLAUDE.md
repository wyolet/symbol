# symbol

AST-native code intelligence for Python. CLI for humans (audit, map, loc) and MCP server for agents (12 symbol-level tools). Static analysis only — never imports or executes target code.

Repo: [`github.com/wyolet/symbol`](https://github.com/wyolet/symbol). Local path: `/Users/abror/projects/wyolet/symbol`. CLI command is `symbol`; PyPI distribution is `wyolet-symbol` (the bare `symbol` name is reserved by PyPI policy). Python is the proving ground; **Go** and **TypeScript** are next on the roadmap (same architecture, different parsers).

## Commands

- **`symbol audit`** — Runs all registered checkers: stack, entrypoints, orphans, side effects, swallowed exceptions, TODOs, unused deps, code structure
- **`symbol loc`** — GitHub Linguist port: 500+ languages, multi-strategy detection (modeline, shebang, filename, extension, XML, manpage)
- **`symbol map`** — Import graph: circular imports, hotspots, fragile modules, deep chains, blast radius
- **`symbol search` / `symbol code` / `symbol outline` / `symbol callers`** — Symbol-level inspection
- **`symbol patch`** — Byte-range edit (replace / delete / insert) without `old_string` payloads
- **`symbol analyze` / `symbol dump`** — Per-file AST analysis
- **`symbol init`** — Generate recommended `[tool.symbol]` config
- **`symbol update-linguist`** — Pull latest language definitions from GitHub
- **`symbol mcp [--root PATH]`** — Run the MCP server (stdio) exposing 12 agent tools: SearchSymbol, SymbolBody, SymbolOutline, SymbolCallers, Patch, MultiPatch, DeleteSymbol, InsertSymbol, RenameSymbol, ReplaceSymbol, Undo, Refresh
- **`symbol undo`** — Revert the most recent Rename/Replace transaction (uses `.symbol/transactions/`; no git involvement)
- **`symbol refresh [--full]`** — Reindex changed files and clear transaction history. Escape hatch when state drifts.

## Claude Code plugin

`plugin/` is installable via `claude plugin install git+https://github.com/wyolet/symbol@main`. It bundles:
- `plugin/.mcp.json` — registers the `symbol mcp` stdio server
- `plugin/skills/symbol/SKILL.md` — steers Claude away from native Read/Grep/Edit on indexed Python files
- `plugin/hooks/hooks.json` — PreToolUse / PostToolUse soft-nudge hooks

The server is plain MCP — it also works with opencode, Cursor, Continue, and anything else that speaks MCP. See pinned GitHub issues for the integration work.

## Structure

```
src/wyolet/                    — namespace package (no __init__.py — PEP 420)
└── symbol/
    ├── cli.py                — Typer root (dispatches; bare-path defaults to audit)
    ├── commands/             — Thin CLI views (audit, loc, map, analyze, search,
    │                          code, outline, callers, patch, hook, refresh,
    │                          undo, init, +symbol-level ops for the MCP surface)
    ├── checkers/             — @register'd checkers (stack, entrypoints, orphans,
    │                          side_effects, swallowed, todos, unused_deps,
    │                          code_structure)
    ├── shared/               — Core infra: AnalysisContext, ASTCache, registry,
    │                          runner, spec/config_resolver, framework_detector,
    │                          graph, symbol_index, simulator, workspace, linguist/
    └── data/
        ├── spec.toml         — Global baseline spec
        └── specs/NAME/       — Per-package specs (237 packages and growing)
```

Imports go `from wyolet.symbol.X import Y`. PyPI distribution is `wyolet-symbol`; future sibling packages (`wyolet-linter`, etc.) install into the same `wyolet/` namespace.

## Architecture

- **Checker registry** (`shared/registry.py`) — `@register(name, kind, ...)` + `views(name, rich=, json=, findings=)`. `kind="file"` runs per file; `kind="project"` runs once. Commands are thin views, not owners.
- **AnalysisContext** (`shared/context.py`) — built once via `build_context()`: project_root, spec, config, ASTCache, frameworks, deps, resolved config. Shared across audit/map/analyze.
- **ASTCache** (`shared/ast_cache.py`) — parses each file once; passed to all consumers.
- **Symbol index** (`shared/symbol_index.py`) — qualified-path → location/signature/body index that backs `search` / `code` / `outline` / `callers` and the MCP read tools.
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
