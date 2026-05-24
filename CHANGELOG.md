# Changelog

All notable changes to `symbol` are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.3] — 2026-05-24

### Added
- **`symbol --version`** — prints the installed version and exits. Reads `importlib.metadata.version("wyolet-symbol")`, so it tracks `pyproject.toml` with no hardcoded string to drift.

### Changed
- **Tagline updated everywhere from "codebase audit toolkit for Python projects" to "AST-native code intelligence for Python and Go"** — CLI `--help`, the package docstring, and the PyPI project description.

## [0.2.2] — 2026-05-24

Documentation and agent-facing strings catch up to the Go support shipped in 0.2.1. No behavior changes beyond one CLI highlighting fix.

### Changed
- **`CLAUDE.md` reframed for multi-language.** Dropped "for Python" from the intro; "Python and Go ship today; TypeScript is next" (was "Go on the roadmap"). Added a **Language coverage** matrix: Go is live for the symbol-level surface (search/outline/body/callers), the rename/write tools, and `loc`; `audit` checkers and the import-graph `map` remain Python-only.
- **`plugin/skills/symbol/SKILL.md` now states Python *and* Go support.** Previously said "Python codebases" / "Python symbols", which steered agents on Go projects away from the MCP tools entirely. The native-tools carve-out no longer excludes Go.
- **`InsertSymbol` / `ReplaceSymbol` MCP descriptions:** "Content must parse as Python" → "must parse in the target file's language".
- **`hook.py` read-nudge tail:** "non-Python regions" → "non-code regions" (the nudge already interpolates `{lang}`, so the hardcoded tail was inconsistent on `.go` files).

### Fixed
- **`symbol patch` preflight panel highlighted Go source as Python.** The syntax lexer was hardcoded to `"python"`; it's now derived from the file path via `Syntax.guess_lexer`.

## [0.2.1] — 2026-05-21

**Closes [#15](https://github.com/wyolet/symbol/issues/15) — `RenameSymbol` is no longer textual.** Both Python and Go now go through AST-based engines with cross-type discrimination, partial-apply with structured reporting, and (Go) `go/types`-backed semantic correctness. Tier-1 regex fallback remains for kinds and languages the engine doesn't cover.

### Added
- **AST-based rename engine for Python** ([#16](https://github.com/wyolet/symbol/pull/16)). Handles `method` / `async_method` / `function` / `async_function` / `class` / `constant`. Resolves receivers via `self`/`cls` → enclosing class, parameter annotations, assignment scans (`b = Foo()`), import aliases, and same-file class declarations. Walks Name + Attribute + alias for module-binding renames; classifies each site as `rewrite` / `skipped_mismatch` / `unresolved` with a human-readable `why`.
- **Scope-aware shadowing detection** for module-binding renames. Skips Name references that are locally rebound (assignment, parameter, for-loop variable, with-as, except-as, nested def, local import of an unrelated symbol). A local `from <target_module> import leaf` is correctly recognized as the *same* target and gets rewritten too — not flagged as a shadow.
- **AST-based rename engine for Go** ([#17](https://github.com/wyolet/symbol/pull/17)) backed by `golang.org/x/tools/go/packages` + `go/types`. Tier-2 from the start since Go ships a stdlib type checker — every limit Python tier-1 documents (factory returns, interface dispatch, embedded-method promotion, generics) is resolved exactly via `info.Selections[sel].Obj()` and `info.Uses[ident]`. Handles cross-package selectors (`pkg.Foo`), value-receiver vs pointer-receiver methods, parent-package re-exports.
- **Interface-contract impact surfacing for Go.** When renaming a method whose receiver type satisfies one or more interfaces, the result surfaces a `affected_interfaces` list with each interface's qualified path and declaration site. Loud signal, never silent auto-extension. Implementation-set rename (gopls-style) is explicitly out of scope.
- **Fail-loudly partial-apply policy.** Result now carries three buckets: `rewrites` (what changed), `skipped_mismatch` (correctly identified as a different declaration), `unresolved` (with `file:line:col` + receiver expression + named `why`). Status becomes `needs_review` when nothing applied but uncertain sites exist. No `--force` flag, no silent skips.
- **`IndexQuery` protocol** (`protocols/index_query.py`) — thin neutral interface (`find_declaration`, `class_bases`, `owners_of_leaf`) so language adapters can ask the index a few questions during rename without importing the index module.
- **Renamer engine** (`writes/rename/`) — language-neutral orchestrator (`SymbolRenamer`) with declaration resolution, candidate-file enumeration, fail-loudly policy, transaction commit. Adapters return `RenameAnalysis` per file; engine aggregates and applies.
- **Re-export detection.** `from pkg import Foo` where `pkg/__init__.py` re-exports `Foo` from `pkg.impl` now rewrites correctly when renaming `pkg.impl.Foo`. Same for Go's transitive imports.
- **`_candidate_files` enumeration also includes import-alias files** — fixes a gap where module-level imports of the leaf weren't tracked as in-body refs and re-export `__init__.py` sites were missed.
- **20 new tests** locking in the engine's behavior — 13 Python (`tests/test_rename_engine_v2.py`), 9 Go (`tests/test_rename_go_engine.py`). Each Go rename test runs `go build ./...` to verify type identity is preserved.

### Changed
- **`RenameSymbol` routes Python methods/functions/classes/constants and Go methods/functions/types/vars/consts through the new engine.** Other kinds and unsupported languages keep the tier-1 regex path.
- **Python adapter: nested functions inside `ClassDef` are now tagged `method` / `async_method`** in the index (was always `function`). Side fix; required for kind-based dispatch but also corrects a latent index bug.
- **Result types extended** with `unresolved`, `skipped_mismatch`, `affected_interfaces`, plus a richer multi-line `message` field that includes per-site `file:line:col` so any client only seeing the summary string still gets actionable pointers.
- **CLAUDE.md: codified the language-isolation invariant** — `import ast`, `tree_sitter`, `go/ast`, `pyright`, etc. live only in `adapters/<lang>/` and never leak into `shared/` / `writes/` / `reads/` / `checkers/` / `commands/` / `mcp/`. Multi-language tools are written once against the adapter protocol.
- **Go daemon takes a `project_root`** for rename operations. Required for `go/types` to operate over a real package set via `packages.Load("./...")`. Other RPC methods still take source bytes; the architectural relaxation only applies to rename.
- **CHANGELOG entry for 0.2.0 had `RenameSymbol is textual (tier-1)` as a known limitation** — that limit is now closed.

### Fixed
- **Cross-type method rename ([#15](https://github.com/wyolet/symbol/issues/15)).** Renaming `foo.Service.Save` no longer rewrites `bar.Service.Save()` call sites. Discriminator resolves receiver types and skips mismatches.
- **String/comment/docstring false positives.** Old `\bleaf\b` regex matched inside string literals, comments, and Python kwargs. AST-driven walks only visit identifier nodes — those bytes are now never touched.
- **`scan_file`'s underlying go-scan binary version bumped to `0.2.1`** so worker handshake reflects new capabilities.

### Known limitations
- **Generics.** No specific test for renaming methods on generic types or generic functions. `go/types` handles them and the algorithm should work via the same Selection/Uses paths, but unverified.
- **Large-project performance.** Go's `packages.Load("./...")` is several seconds on big monorepos. Result is cached per rename op (so the first per-file call pays, subsequent calls are free), but no cross-rename caching. Incremental package loading + scope filtering is a follow-up.
- **Implementation-set rename (gopls-style multi-rename of an interface's contract method + all implementers).** Surfaced as `affected_interfaces` for review; no auto-extension. Out of scope for 0.2.1.
- **Python shadowing analysis** doesn't handle comprehension scope (`[save for save in items]`), walrus `:=` outside an enclosing statement, or `global` / `nonlocal` declarations. Rare in practice; rewrite still produces valid Python because Name walker covers identifier nodes only — but the shadowing may not be detected and over-rewrites are possible in these rare patterns.
- **Python `super().X`** receiver resolution is unimplemented — needs `class_bases` in the index. Surfaces as `unresolved` with a clear why.
- **Python factory-return receivers** (e.g. `y = make_thing(); y.X()`) — unresolved without a type checker. Tier-2 pyright integration is a future addition. (Go already gets this for free via `go/types`.)
- **`field` / `local` / `parameter` kinds** aren't emitted by the Python index yet; dispatch stubs return `kind_not_supported`.
- **Go-only:** `pop_affected_interfaces` is one-shot per (project_root, target_qpath). Renaming the exact same target twice in one process loses the second call's interface impact data. Not a real-world concern; CLI invocations don't repeat.

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

[Unreleased]: https://github.com/wyolet/symbol/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/wyolet/symbol/releases/tag/v0.2.1
[0.2.0]: https://github.com/wyolet/symbol/releases/tag/v0.2.0
[0.1.2]: https://github.com/wyolet/symbol/releases/tag/v0.1.2
[0.1.1]: https://github.com/wyolet/symbol/releases/tag/v0.1.1
[0.1.0]: https://github.com/wyolet/symbol/releases/tag/v0.1.0
