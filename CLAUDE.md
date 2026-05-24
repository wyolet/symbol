# symbol

AST-native code intelligence. CLI for humans (audit, map, loc) and MCP server for agents (12 symbol-level tools). Static analysis only — never imports or executes target code. Python is fully covered; **Go** is live for the symbol-level surface (search/outline/body/callers), the rename/write tools, and loc — audit checkers and the import-graph map remain Python-only (see Language coverage below).

Repo: [`github.com/wyolet/symbol`](https://github.com/wyolet/symbol). Local path: `/Users/abror/projects/wyolet/symbol`. CLI command is `symbol`; PyPI distribution is `wyolet-symbol` (the bare `symbol` name is reserved by PyPI policy). Python and **Go** ship today; **TypeScript** is next on the roadmap (same architecture, different parsers).

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

Imports go `from wyolet.symbol.X import Y`. PyPI distribution is `wyolet-symbol`. The `wyolet/` namespace is PEP 420 so future sibling packages can install alongside without an `__init__.py` collision.

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

## Language coverage

| Capability | Python | Go |
|---|---|---|
| Index / `search` / SearchSymbol | ✅ | ✅ |
| `code` / `outline` / `callers` (read tools) | ✅ | ✅ |
| `patch` + rename/replace/insert/delete (write tools) | ✅ | ✅ |
| `loc` | ✅ | ✅ |
| `audit` checkers (stack, orphans, side_effects, …) | ✅ | ❌ |
| `map` / import graph | ✅ | ❌ |

Go is daemon-backed (`go-scan` over `go/types`, bundled binaries built in CI) and registered at priority 10 in `adapters/registry.py`; whole-project rename runs in a single RPC, so cross-file/interface-impact resolution is richer than Python's per-file AST walk. The Go gaps — audit checkers and the import-graph map — are hardwired to stdlib `ast`/`ASTCache` and have no adapter abstraction yet; closing them means routing those through the adapter protocol, not adding `if lang == "go"` branches.

## Language isolation — non-negotiable

**Language-specific code lives in `adapters/<lang>/` and never leaks out.**

- `import ast`, `tree_sitter`, `go/ast`, `pyright`, or any other language-specific parser/runtime is allowed **only** inside `adapters/<lang>/`. Never in `shared/`, `writes/`, `reads/`, `checkers/`, `commands/`, `mcp/`, or any other neutral layer.
- Neutral layers operate on the index, on `Hit`/`Ref`/`Symbol` records the adapter produces, and on the adapter protocol. They never branch on language and never parse source.
- Multi-language tools (rename, callers, search, patch) are written **once** against the adapter protocol. Adding a new language = new adapter, not new branches in the engine.
- When a write/read operation needs language-aware behavior (receiver resolution, scope walking, ref discovery), extend the adapter protocol with a method and implement it per-language. Never put language logic behind an `if lang == "python"` in a neutral layer.

If you catch yourself writing `import ast` outside `adapters/`, stop — the right move is a method on the adapter protocol.

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
