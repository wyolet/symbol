# Changelog

All notable changes to `symbol` are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] — 2026-05-21

First release with **multi-language support**. Go projects are now indexed and queryable through every read tool (`search`, `outline`, `code`, `callers`, `todos`, `loc`) and most write tools (`Patch`, `MultiPatch`, `DeleteSymbol`, `InsertSymbol`, `RenameSymbol`, `ReplaceSymbol`) via a JSON-RPC daemon written in Go. Symbol's architecture is no longer Python-only.

### Added
- **Go language adapter** — full read-side support and most writes. Backed by a small `go-scan` daemon (zero external Go dependencies) that the Python `GoAstAdapter` drives over JSON-RPC 2.0. Wheels bundle prebuilt binaries for darwin/linux/windows × arm64/amd64; CI cross-compiles the matrix. Dev builds: `make build-go-scan`.
- **Adapter tiering** — `LanguageRegistry.for_language` walks the priority bucket and returns the first adapter where `is_enabled` is True. Adapters self-report enablement; their class docstring is the install contract. Sets up the LSP/AST/tree-sitter ladder for future semantic adapters.
- **Language-adapter RPC protocol** — `schemas/symbol.rpc.{schema,methods}.json` define the wire format and method registry, hand-synced with Python dataclasses (`protocols/types.py`) and Go structs (`adapters/go_ast/daemon/internal/rpc/types.go`). CI validates round-trips on the Python side. Host-agnostic by design: the same protocol applies whether Python or Go is the orchestrator.
- **`go.mod`-aware module paths** — Go symbols' qualified paths come from the nearest `go.mod`'s `module` directive plus the file's relative directory, matching real Go import paths.
- **`preview` on the `LanguageAdapter` protocol** — was an implicit contract; now declared, so search previews are guaranteed to work on every adapter.

### Changed
- **Linguist owns file discovery (#13).** `Linguist.file_languages` is the single source of truth for "which files exist and what language each one is." `ASTCache` reads from this map; `_git_tracked_py` became `_git_tracked_sources` (no extension filter); the audit/index pipeline shares one linguist instance with `loc`. No `*.py` glob anywhere outside linguist's own data.
- **Adapters compute signatures via AST, not strings.** `PythonAstAdapter` uses `ast.parse` + node-shape builders; `GoAstAdapter` calls a daemon `signature` RPC that uses `go/parser` + `go/printer` (FuncDecl body stripped). Output matches `inspect.signature` / gopls hover. The hand-rolled state machines that scanned for `:` and `{` are gone.
- **Method renamed:** `LanguageAdapter.signature_from_text` → `LanguageAdapter.signature`. Implementation detail no longer leaked through the API name.
- **`module_prefix(rel_path)` → `module_prefix(path, project_root)`** on the protocol. Adapters get the absolute file path and project root so they can read manifests (e.g. `go.mod`) instead of guessing from path components.
- **`signature()` on Go strips the body-opening `{`** — `func (c *Command) Execute() error`, not `func (c *Command) Execute() error {`. Matches gopls/godoc convention.
- **MCP target classification is language-agnostic.** `symbol_body` / `symbol_outline` try the read function (which queries the index — language-agnostic) first; only on miss do they fall back to "is this a file on disk?" (the one neutral test). The old slash-as-file heuristic — Python-shaped and broken for Go import paths — is gone.
- **`is_enabled` is required on every adapter.** `PythonAstAdapter` declares it explicitly (`True` — no toolchain to check). The registry no longer accepts adapters that omit it.

### Fixed
- **`signature()` on selector tails.** Go's scan walker recorded the trailing identifier of a SelectorExpr as both `attr` and `name`; now only `attr`. Regression net in tests.
- **JSON null vs `[]`.** Go's nil slices marshal to `null`; the Python deserializer was strict and crashed. Daemon initializes refs as empty slices; client tolerates either form.
- **`replace_symbol` rejected bare Go snippets** (no `package` declaration). The adapter now transparently wraps with `package _stub` for `validate_syntax`/`symbols`, shifts byte/line offsets back to caller coordinates. The Python `ast` module already accepts bare module-level code; Go's stricter rule no longer leaks through the writes API.
- **Daemon process leak.** `GoAstAdapter` now sends a `shutdown` notification and reaps the subprocess on Python exit via `atexit` + `__del__`. No more orphan `go-scan` processes.

### Removed
- **`shared/loc_counter.py`** (196 lines + tests). Parallel LOC implementation with its own hardcoded extension table and `rglob` walker — fully superseded by linguist's `detect_directory`. Dead code with tests was the only thing importing it.
- **`shared/files.collect_py_files`** → replaced by `filter_paths` which only filters a caller-supplied iterable. No walking, no extension matching.
- **String state machines in adapters.** Both `PythonAstAdapter.signature_from_text` and `GoAstAdapter.signature_from_text` were hand-rolled parsers scanning for `:` and `{`. Replaced with AST-driven extraction.

### Known limitations
- **`RenameSymbol` is textual (tier-1).** It matches the trailing identifier of selector expressions, so renaming `foo.Service.Save` also rewrites unrelated `bar.Service.Save()` calls that share the leaf name. Tracked in [#15](https://github.com/wyolet/symbol/issues/15); fix lands in a follow-up release (AST receiver-type inference → `go/types` for full coverage). Use dry-run + manual review for cross-package method names like `Save`, `Get`, `Set`, `Close`.
- **No Go dependency parsing yet.** `go.mod` isn't read by the stack/deps system; `symbol audit` shows "no recognized dependencies" on Go projects. `adapters/go_deps.py` is the planned shape.
- **No Go-native checkers yet.** `symbol audit` runs only the Python and language-agnostic checkers; `func main` detection, Go orphan analysis, and unused-import detection are deferred.

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

[Unreleased]: https://github.com/wyolet/symbol/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wyolet/symbol/releases/tag/v0.2.0
[0.1.2]: https://github.com/wyolet/symbol/releases/tag/v0.1.2
[0.1.1]: https://github.com/wyolet/symbol/releases/tag/v0.1.1
[0.1.0]: https://github.com/wyolet/symbol/releases/tag/v0.1.0
