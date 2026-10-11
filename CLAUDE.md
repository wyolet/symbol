# symbol

AST-native code intelligence: a CLI for humans (`audit`, `map`, `loc`, symbol inspection) and an MCP server for coding agents (symbol-level read/write tools). Static analysis only — **never** import or execute the target code.

CLI command is `symbol`; PyPI distribution is `wyolet-symbol` (the bare `symbol` name is reserved by PyPI policy). Python is the primary target; Go is supported at the symbol-level read/write surface. Install, the full command reference, and the MCP tool list live in the README — this file is the never-violate rules only.

## Language isolation — non-negotiable

**Language-specific code lives in `adapters/<lang>/` and never leaks out.**

- `import ast`, `tree_sitter`, `go/ast`, `pyright`, or any other language-specific parser/runtime is allowed **only** inside `adapters/<lang>/`. Never in `shared/`, `checkers/`, `commands/`, or any other neutral layer.
- Neutral layers operate on the index and on the `Hit`/`Ref`/`Symbol` records the adapter produces. They never branch on language and never parse source.
- Multi-language tools (rename, callers, search, patch) are written **once** against the adapter protocol. Adding a language is a new adapter, not new branches in the engine.
- Language-aware behavior (receiver resolution, scope walking, ref discovery) is a method on the adapter protocol, implemented per-language — never an `if lang == "python"` in a neutral layer.
- Where a capability is single-language today, closing the gap means routing it through the adapter protocol, not adding a language branch.

If you catch yourself writing `import ast` outside `adapters/`, stop — the right move is a method on the adapter protocol.

The Rust core (`rust/symbol`) follows the same rule: language analysis runs only in the language daemons (pyscan from `adapters/`, go-scan from `adapters/go_ast/daemon`); index, reads, writes, MCP, and hook never branch on language.

## Architecture layering — never violate

- **Checker registry** (`shared/registry.py`) — `@register(name, kind, ...)` + `views(...)`. `kind="file"` runs per file; `kind="project"` runs once. Commands are thin views, not owners.
- **AnalysisContext** (`shared/context.py`) — built once via `build_context()`, shared across audit/map/analyze. Never rebuilt per-checker.
- **ASTCache** (`shared/ast_cache.py`) — each file parsed once, passed to all consumers.
- **Symbol index** (`rust/symbol/src/index.rs`) — qualified-path → location/signature/body; backs the Rust binary's reads, writes, and MCP tools.
- **Spec system** (`shared/spec.py`, `shared/config_resolver.py`) — global baseline (`data/spec.toml`) → per-package specs (`data/specs/NAME/`, loaded only when the package is a project dep) → project config (`symbol.toml` or `[tool.symbol]` in pyproject). Namespaces: `[checkers.orphan]`, `[checkers.side_effects.*]`, `[checker]`, `[scanner]`.
- **Pipeline hooks** (`shared/pipeline.py`) — `@hook(pipeline, priority)` for DEPS/SKIP_ORPHAN/ENTRYPOINTS/IMPORTS. Framework-specific logic lives in package specs, not core checkers.

## Conventions

- Python 3.13. **Never** `from __future__ import annotations` — native annotations only.
- CLI: Typer (Python commands), clap (Rust binary). Package manager: uv. Format + lint: ruff (handles import sorting).
- **Never hand-edit `pyproject.toml` for deps** — use `uv add` / `uv remove` / `uv sync --all-groups`.
- **No inline scripts** — save to `.tmp/<name>.py` and run `uv run .tmp/<name>.py`.
- Terminology: **checker** (matches ruff/pylint), not "detector".
- Package-specific filenames (admin.py, urls.py) belong in package specs, never the global spec — global is Python/community conventions only (app.py, main.py, conftest.py, …).
- `__init__.py` re-exports are conventions — not cycles, not hotspots.
- Output: compact by default, `-v` for detail, `--format json` for CI.

## Design principles

- Static analysis only — never import or execute target code.
- Minimal false positives over completeness.
- One command per concern.
- Useful within 30 seconds of encountering an unfamiliar codebase.
- CI-friendly: exit 1 on errors, JSON for machines.
