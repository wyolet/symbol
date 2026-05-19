# Changelog

All notable changes to `symbol` are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.2] — 2026-05-19

### Changed
- **Env vars renamed** to the `SYMBOL_*` prefix: `CA_AGENT` → `SYMBOL_AGENT`, `CA_SESSION_ID` → `SYMBOL_SESSION_ID`, `CA_MCP_SESSION` → `SYMBOL_MCP_SESSION`. The old names are no longer recognized.
- `symbol audit` output: `unused_deps` no longer suggests a non-existent `ca deps` command; verbose mode (`-v`) lists the unused deps inline instead.
- `symbol dump` default output file: `ca-analysis.json` → `symbol-analysis.json`.

### Fixed
- `fastapi` package spec: skip public re-export submodules from orphan detection (`fastapi/middleware/*`, `fastapi/staticfiles.py`, `fastapi/templating.py`, `fastapi/exception_handlers.py`). Widen side-effect skip list to cover `add_middleware`, `add_exception_handler`, `mount`, `model_rebuild`. Demote findings under `tests/**` to debug.

## [0.1.1] — 2026-05-19

### Changed
- Distribution renamed to `wyolet-symbol` (the bare `symbol` name is reserved by PyPI policy). CLI command remains `symbol`.
- First release published to PyPI.
- Added GitHub Actions release workflow with trusted publishing (OIDC).
- Added `LICENSE`, `CHANGELOG.md`, and CI workflow.

## [0.1.0] — 2026-05-19

First tagged release.

### Added
- **Audit CLI**: `symbol audit`, `symbol map`, `symbol loc`, `symbol analyze`, `symbol dump`, `symbol init`, `symbol update-linguist`.
- **Symbol-level inspection**: `symbol search`, `symbol code`, `symbol outline`, `symbol callers`.
- **Edit surface**: `symbol patch` byte-range edits with read-cache + transactional `symbol undo` / `symbol refresh`.
- **MCP server** (`symbol mcp`) exposing 12 agent tools: `SearchSymbol`, `SymbolBody`, `SymbolOutline`, `SymbolCallers`, `Patch`, `MultiPatch`, `DeleteSymbol`, `InsertSymbol`, `RenameSymbol`, `ReplaceSymbol`, `Undo`, `Refresh`.
- **Claude Code plugin** (`plugin/`) bundling MCP server registration, the `symbol` skill, and PreToolUse / PostToolUse soft-nudge hooks.
- **Checkers**: `stack`, `entrypoints`, `orphans`, `side_effects`, `swallowed`, `todos`, `unused_deps`, `code_structure`.
- **237 package specs** under `src/wyolet/symbol/data/specs/` covering Django, FastAPI, Celery, SQLAlchemy, LangChain, Pydantic, pytest, and 230+ others.
- **GitHub Linguist port**: 500+ languages with real GitHub colors, multi-strategy detection (modeline, shebang, filename, extension, XML, manpage).

[Unreleased]: https://github.com/wyolet/symbol/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/wyolet/symbol/releases/tag/v0.1.2
[0.1.1]: https://github.com/wyolet/symbol/releases/tag/v0.1.1
[0.1.0]: https://github.com/wyolet/symbol/releases/tag/v0.1.0
