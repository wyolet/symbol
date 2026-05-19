# Changelog

All notable changes to `symbol` are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/wyolet/symbol/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wyolet/symbol/releases/tag/v0.1.0
