# Write architecture — protocol-first design

The write surface is built protocol-first: define interfaces, implement concretely for Python/tier-1, swap in new languages and resolution tiers without touching the pipeline or commands.

Status: design in progress. This doc starts from the highest level (operations and their semantics) and works down to protocols and implementations.

## Design principle

> **Never import a concrete adapter in the pipeline or in a write resolver. Always go through the protocol.** If you can't, the protocol is missing a method — add it, implement it in the adapter.

This prevents the rebuild that would otherwise happen when multi-language or tier-2 resolution arrives.

---

## Level 0: Operations and semantics

The write surface exposes five operations. Each has a clear semantic contract independent of language, resolution tier, or implementation details.

### `patch`

**Semantic:** Replace a byte range in a file with new content.

**Contract:**
- Input: `(file, range, content)`. Content determines effect: non-empty = replace, empty = delete, zero-width range = insert.
- Precondition: file exists, range is within bounds, agent has seen the target bytes (read cache) or accepts confirmation.
- Postcondition: file contains new content at the specified range. Lines before and after the range are unchanged.
- Invariant: one file, one range, one write. No cross-file effects.
- Safety: atomic per-file write. Parse-verify optional but recommended.

**This is the universal primitive.** Every other operation composes patches.

### `rename-symbol`

**Semantic:** Change a symbol's name everywhere it appears in the codebase.

**Contract:**
- Input: `(old_qualified_path, new_name)`.
- Precondition: symbol exists in the index. No name collision at declaration scope. Working tree is clean (or `--allow-dirty`).
- Postcondition: the symbol's declaration and all detected references use the new name. The symbol's identity (kind, body, parameters) is unchanged — only the name differs.
- Invariant: the set of references updated depends on the resolution tier. Tier-1 (textual) may miss shadowed names or touch false positives. Tier-2 (semantic) is scope-correct.
- Safety: multi-file transaction with git checkpoint. Unresolved/ambiguous references are reported, not silently skipped or applied.

**Rename is the highest-value AST-aware write.** It replaces N file reads + N edits with one command.

### `replace-symbol`

**Semantic:** Replace a symbol's full definition (including signature) with new content. If the new content declares a different name, update all references automatically.

**Contract:**
- Input: `(qualified_path, content)` where content is a full symbol definition.
- Precondition: symbol exists in the index. Content parses and contains exactly one top-level definition of the same kind as the target. Working tree is clean (or `--allow-dirty`).
- Postcondition: the target's byte range is replaced with the new content. If the new declared name differs from the old leaf name, all references are updated to the new name (same tier as `rename-symbol`). Body change and ref updates commit in one transaction.
- Invariant: atomicity — there is no intermediate state where the body is new but references are stale.
- Safety: single-file when name unchanged. Multi-file transaction when name changes. Git checkpoint. Post-op hints report signature-change consequences (added/removed/reordered parameters that may break callers).

**When it wins:** the "rewrite and rename" pattern. Currently two calls (`rename-symbol` + `patch`); replace-symbol does both atomically in one. Also guarantees body and ref-graph land together — relevant for code that must always parse cleanly (agent-driven CI, staging deploys mid-session).

**Refusals:**
- Content has zero or multiple top-level defs → `invalid_argument`.
- Kind mismatch (replacing a function with a class) → `invalid_argument`.
- Name collision with a sibling symbol → `name_collision`.
- Content doesn't parse → `parse_broken`.

### `move-symbol`

**Semantic:** Relocate a symbol from one file to another.

**Contract:**
- Input: `(qualified_path, new_qualified_path)`. File is derived from the qualified path.
- Precondition: symbol exists. No name collision in destination file. Working tree is clean.
- Postcondition: symbol exists in the destination file with identical body. Symbol is removed from the source file. Import statements across the codebase are NOT updated (v1 — agent drives import fix-up informed by post-op hints).
- Invariant: the symbol's body is byte-identical between source removal and destination insertion. No transformation applied.
- Safety: multi-file transaction (source + destination). Git checkpoint. Post-op analysis reports orphaned imports (source), unresolved names (destination), and stale callers.

**Move is relocation, not transformation.** Import surgery is explicitly out of scope for v1 and lives in post-op hints.

### `delete-symbol`

**Semantic:** Remove a symbol from the codebase.

**Contract:**
- Input: `(qualified_path)`.
- Precondition: symbol exists. No live references unless `--force` or `--with-refs`.
- Postcondition: symbol's byte range is removed from its file. With `--with-refs`: all detected references are also removed. With `--with-imports`: unused imports in the same file are cleaned up.
- Invariant: deletion is exact to the symbol's AST-determined byte range. No neighboring code is affected.
- Safety: single-file for base case. Multi-file if `--with-refs`. Git checkpoint on multi-file. Reports what was removed and what still references the deleted symbol.

### `insert-symbol`

**Semantic:** Add new code at a position anchored to an existing symbol.

**Contract:**
- Input: `(anchor_path, position, content)`. Position is one of: `before`, `after`, `start`, `end`.
- Precondition: anchor symbol exists. `start`/`end` only valid for symbols with a body (class, function).
- Postcondition: new content appears at the specified position relative to the anchor. Indentation matches the anchor's scope (configurable via `--no-reindent`).
- Invariant: the anchor symbol's own body is unchanged. Only whitespace/position between symbols is affected.
- Safety: single-file. Atomic write. Parse-verify recommended.

---

## Cross-cutting semantics

These apply to all operations uniformly.

### Output contract

Every operation returns a structured result with at minimum:
- `status`: `applied | needs_read_confirmation | conflict | invalid`
- `file(s)` affected
- `before` / `after` state (ranges, line counts)
- `undo` instruction

Agent-mode (`--agent`, implicit in MCP) enriches the text response with:
- Post-op analysis hints (orphan imports, unresolved names, stale callers)
- Suggested next commands
- Human-readable summary of what changed

JSON mode returns bare facts. Text mode renders for the consumer.

> **The operation is clean. The telling is opinionated.**

### Transaction semantics

| Scope | Operations | Mechanism |
|---|---|---|
| Single-file | `patch`, `delete-symbol` (base), `insert-symbol` | Atomic per-file rename |
| Multi-file | `rename-symbol`, `replace-symbol` (when name changes), `move-symbol`, `delete-symbol --with-refs` | Two-phase commit + git checkpoint |

Multi-file operations:
- Require git. Refuse on non-git projects unless `--force-no-vcs`.
- Require clean working tree (or `--allow-dirty`).
- Create a checkpoint commit before writing.
- Roll back automatically on Phase 2 failure.
- Undo: `git reset --hard HEAD~1`.

### Read-cache semantics

The read cache tracks what bytes the agent has seen (see `read-cache.md`). Operations check it before writing:
- **Agent saw the range** → apply.
- **Agent didn't see it** → `needs_read_confirmation` with current content + confirm token.
- **Agent saw it, but file changed since** → conflict or silent refresh depending on whether target bytes changed.

### Resolution tier semantics

Operations that find references (`rename-symbol`, `replace-symbol` when name changes, `delete-symbol --with-refs`) accept a tier:
- **Tier-1 (textual):** string match on qualified path + name. Fast, some false positives. Default.
- **Tier-2 (semantic):** scope-aware. Requires `LanguageAdapter.scope_of()`. Future.
- **Tier-3 (type-aware):** type-resolved references. Requires type info. Far future or LSP delegation.

The operation's response includes which tier was used. Higher tiers are opt-in (`--tier semantic`).

### Symbol kinds and safety

| Kind | rename | replace | move | delete | insert-symbol |
|---|---|---|---|---|---|
| class | ✓ | ✓ | ✓ | ✓ | ✓ (before/after/start/end) |
| function (module) | ✓ | ✓ | ✓ | ✓ | ✓ (before/after) |
| method | ✓ | ✓ | — | ✓ | ✓ (before/after within class) |
| constant (module) | ✓ | ✓ | ✓ | ✓ | ✓ (before/after) |
| local variable | refuse (tier-1) | — | — | — | — |
| parameter | refuse (tier-1) | — | — | — | — |

Local/parameter rename requires `--scope file` or `--tier semantic`. This is a safety constraint, not a limitation.

---

## Command surface

Consolidated CLI shape for all six operations. These are the canonical forms; MCP tool schemas mirror them directly.

```
ca patch <file> --range <A>-<B> [--content <text>]
ca patch <file> --confirm <token>

ca rename-symbol <qualified-path> <new-name>
  --scope project|dir|file           (default: project)
  --tier textual|semantic            (default: textual)

ca replace-symbol <qualified-path> [--content <text>]
  --tier textual|semantic            (used only when name changes)

ca move-symbol <qualified-path> --to <new-qualified-path>

ca delete-symbol <qualified-path>
  --with-refs
  --with-imports
  --force
  --tier textual|semantic

ca insert-symbol --anchor <qualified-path> --position before|after|start|end
  [--content <text>]
  --no-reindent

# Shared flags (all commands):
  --dry-run                          Preview only; default is apply
  --allow-dirty                      Allow uncommitted working tree on multi-file ops
  --agent                            Enriched output (implicit in MCP)
  --format text|json                 (default: text)
```

### Conventions

- **`--content` is the consistent name for code payloads** across all commands. Not `--with`. Self-describing.
- **Content delivery**: explicit via `--content <text>`, or piped via stdin when `--content` is absent in CLI. MCP always passes content as a parameter field (stdin is occupied by the JSON-RPC transport).
- **Apply is the default**, not dry-run. Git checkpoint is the safety net for multi-file ops. CLI can override with `--dry-run`. MCP server can override globally via config:
  ```
  [writes]
  mode = "apply"                      # "apply" | "dry-run"
  confirm_threshold = 5               # dry-run if touching > N files
  ```
  Start conservative while agents learn the tool, open up as trust grows.
- **Per-operation safety flags stay per-operation.** `--force`, `--with-refs`, `--allow-broken` are not global — each command earns its own.

### Rename constraints

`<new-name>` is a **leaf name only** — no dots, no slashes. Renaming cannot change the containing module:

```
ca rename services.user.UserService NewUserService          # ✓
ca rename services.user.UserService models.user.NewUser     # ✗ error
```

Validation rejects dotted names with a pointer to `ca move`. This prevents agents from using rename when they meant move.

### Rename and move in v1

Kept as separate commands. Rationale:
- Rename updates refs across the codebase.
- Move (v1) does NOT update imports — explicitly deferred.
- Merging them means either move inherits ref-update complexity or rename loses ref updates.

When move gets `--update-imports` in a later version, rename becomes a degenerate case of same-file move. At that point, rename stays as a convenience alias, but the two are semantically convergent.

### CLI-first, MCP as thin wrapper

The CLI is the source of truth. `ca patch`, `ca rename`, etc. work from any shell. The MCP server is a thin wrapper that calls the same resolvers — same code path, different transport.

Consequences:
- Write work doesn't block on MCP server implementation.
- Agents can drive writes via Bash today, via MCP when the server ships.
- MCP adds session-scoped read cache + structured response parsing. Not required for correctness.

### Agent calls: MCP vs Bash

Both work. The agent chooses per call based on what's cheapest:

| Operation | Preferred transport | Why |
|---|---|---|
| `ca search`, `ca code`, `ca outline`, `ca callers` | MCP | Small input, large structured response — JSON overhead is negligible, structured response is valuable |
| `ca patch`, `ca insert-symbol` | Bash heredoc (when available) | Content is the main payload; heredoc avoids JSON escaping of code |
| `ca rename-symbol`, `ca move-symbol`, `ca delete-symbol` | Either | No content payload; args are small |
| `ca replace-symbol` | Bash heredoc (when available) | Content is the full symbol definition — heredoc avoids JSON escaping |

Heredoc example:
```bash
ca patch src/user.py --range 10-20 <<'CONTENT'
def save(self):
    self.db.write(self.user)
    return True
CONTENT
```

No escaping, actual newlines, no JSON boilerplate on the input side.

---

## Output schema

Every write operation returns a structured result. JSON is the canonical shape; text renderings are derived. The schema is the contract between the operation and any consumer (agent, script, CI).

### Top-level status enum

Exactly one of these, always present:

| Status | Meaning | Disk touched? | Agent next action |
|---|---|---|---|
| `applied` | Op completed, changes written | Yes | Continue |
| `dry_run` | Op would apply; returned diff without writing | No | Inspect diff, re-run with `--apply` (or MCP mode set to `apply`) |
| `needs_read_confirmation` | Agent hasn't seen target content | No | Read the included content, call back with `--confirm <token>` or send updated op |
| `conflict` | Target bytes differ from what we served to agent | No | Inspect `current_content`, decide to retry or abandon |
| `error` | Op refused for a specific reason | No | See `error_code`, remediate, retry |

### Error codes

When `status = error`, `error_code` is always set:

| Code | Meaning |
|---|---|
| `file_not_found` | Target file doesn't exist |
| `permission_denied` | OS refused write |
| `binary_file` | File is not text / not UTF-8 |
| `range_out_of_bounds` | Range exceeds file length or is negative |
| `symbol_not_found` | Qualified path doesn't resolve in the index |
| `symbol_ambiguous` | Qualified path matches multiple symbols |
| `name_collision` | Destination already has a symbol with that name |
| `parse_broken` | Post-patch content fails parse-verify (unless `--allow-broken`) |
| `cycle_detected` | Op would introduce a circular import (future, move) |
| `working_tree_dirty` | Multi-file op refused because git has uncommitted changes |
| `no_git_repository` | Multi-file op refused because project isn't a git repo |
| `invalid_argument` | CLI/schema validation failed (e.g. dotted name on `rename-symbol`) |
| `internal_error` | Bug in ca-tools; includes stack trace in `details.trace` |

Agents should branch on `error_code`, not on `message`. Messages are human-facing and may change.

### Common fields (all statuses)

```json
{
  "status": "applied",
  "command": "patch" | "rename-symbol" | "replace-symbol" | "move-symbol" | "delete-symbol" | "insert-symbol",
  "elapsed_ms": 47,
  "tool_version": "ca-tools 0.8.0"
}
```

### `applied` / `dry_run` shape

```json
{
  "status": "applied",
  "command": "patch",
  "elapsed_ms": 47,
  "tool_version": "ca-tools 0.8.0",
  "files": [
    {
      "file": "src/services/user.py",
      "before": { "range": [120, 145], "hash": "a3f9b2", "lines": 26 },
      "after":  { "range": [120, 162], "hash": "7c12de", "lines": 43 },
      "lines_added": 17,
      "lines_removed": 0,
      "reparse": "ok"
    }
  ],
  "diff": "--- a/...\n+++ b/...\n@@ ...",
  "hints": [],
  "undo": "git reset --hard HEAD~1"
}
```

For `dry_run`: same shape, but `status = "dry_run"`, no `undo` (nothing to undo), `diff` is the would-be change.

Multi-file operations (`rename-symbol`, `replace-symbol` when name changes, `move-symbol`, `delete-symbol --with-refs`) have multiple entries in `files[]`. The `diff` field aggregates all file diffs.

**Per-command extension fields** go alongside `files[]`:

```json
// rename additionally includes:
"symbol": {
  "kind": "class",
  "old_path": "services.user.UserService",
  "new_path": "services.user.NewUserService",
  "signature": "class NewUserService(Base):",
  "declared_at": "src/services/user.py:42"
},
"references_updated": 23,
"unresolved": [...],
"tier": "textual"

// replace-symbol additionally includes:
"symbol": {
  "kind": "function",
  "old_path": "services.user.UserService.save",
  "new_path": "services.user.UserService.persist",
  "signature": "def persist(self, user: User, *, retries: int = 3) -> UserRecord:",
  "declared_at": "src/services/user.py:42"
},
"name_changed": true,
"references_updated": 23,
"signature_changed": true,
"tier": "textual"

// move additionally includes:
"symbol": { "kind": "class", "old_path": "...", "new_path": "..." },
"source": { "file": "...", "range": [42, 89] },
"destination": { "file": "...", "line": 15, "created": false }

// delete additionally includes:
"symbol": { "kind": "function", "path": "services.user.UserService.save" },
"with_refs": false,
"orphaned_callers": [...]

// insert-symbol additionally includes:
"anchor": { "path": "...", "position": "after" },
"inserted_at": "src/services/user.py:56"
```

### `needs_read_confirmation` shape

```json
{
  "status": "needs_read_confirmation",
  "command": "patch",
  "elapsed_ms": 3,
  "file": "src/services/user.py",
  "range": [120, 145],
  "current_content": "def save(self):\n    ...",
  "confirm_token": "ck_a3f9b2",
  "expires_in_seconds": 60,
  "instructions": "If the patch is still correct given current_content, call back with --confirm ck_a3f9b2. Otherwise send an updated patch."
}
```

### `conflict` shape

```json
{
  "status": "conflict",
  "command": "patch",
  "elapsed_ms": 5,
  "file": "src/services/user.py",
  "range": [120, 145],
  "reason": "range bytes changed since served (served hash a3f9, current hash 8b22)",
  "current_content": "def save(self):\n    # file changed on disk\n    ...",
  "proposed_content": "def save(self):\n    self.db.write(self.user)\n    return True",
  "staged_at": ".ca-tools/staging/patch-20260416-a3f9.py",
  "suggested_actions": [
    "re-read the range and send updated patch",
    "apply --force if you accept overwriting the current content",
    "abandon"
  ]
}
```

### `error` shape

```json
{
  "status": "error",
  "command": "rename",
  "error_code": "symbol_not_found",
  "elapsed_ms": 2,
  "message": "No symbol 'services.user.UsreService' found (did you mean 'UserService'?)",
  "details": {
    "searched_for": "services.user.UsreService",
    "suggestions": ["services.user.UserService"]
  }
}
```

For `internal_error`, `details.trace` contains the stack trace.

### Hints (agent-mode only)

The `hints` field carries post-op analysis findings. Present in agent-mode output (text and JSON both); absent or empty in non-agent mode.

```json
"hints": [
  {
    "kind": "orphan_imports",
    "severity": "info",
    "file": "src/services/user.py",
    "items": [
      { "line": 3, "text": "from services.models import User" },
      { "line": 4, "text": "from services.validators import validate_email" }
    ],
    "suggestion": "ca patch src/services/user.py --range 3-4 --with \"\""
  },
  {
    "kind": "stale_callers",
    "severity": "warn",
    "count": 7,
    "suggestion": "ca callers services.user.UserService"
  }
]
```

Hint kinds: `orphan_imports`, `unresolved_names`, `stale_callers`, `empty_file`, `parse_near_miss`, etc. Severity: `info` | `warn`. Never `error` — hints don't fail ops.

### Text rendering

Text output is derived from the JSON schema via command-specific templates. The top of this doc and `refactoring.md` show examples per command. The rule:
- Lead with a one-line summary (status + what happened).
- Key facts on the next few lines.
- Hints indented under an icon (`⚠️` warn, `ℹ️` info).
- Undo instruction on the last line.

Text mode has a Rich variant for TTY (humans) and a plain variant for `--agent` (LLMs).

---

## Exit codes

CLI contract for scripts and for agents using the Bash transport:

| Code | Meaning |
|---|---|
| `0` | `applied` or `dry_run` |
| `1` | `error` (any `error_code`) |
| `2` | `needs_read_confirmation` or `conflict` (retryable — call again with updated input) |
| `64` | Usage error (bad CLI args, unknown flag) — standard `EX_USAGE` |

Agents calling via Bash can branch:
```bash
ca patch ... || case $? in
  1) echo "error, see output" ;;
  2) echo "retryable, handle confirm/conflict" ;;
  64) echo "bad args" ;;
esac
```

---

## Idempotency and concurrency

### Idempotency

`ca patch` is **not idempotent by default**. Calling the same patch twice on the same range has two possible outcomes depending on read-cache state:

1. **First call succeeds, second call is a no-op** — if the second call's content is byte-identical to what's now on disk and the range was served by the first call's response.
2. **First call succeeds, second call is `needs_read_confirmation`** — if the read cache was evicted or the session rotated between calls.

Neither case corrupts data. Agents that want guaranteed-idempotent behavior can check the response's `before.hash` and skip if it matches the content they'd patch to.

`ca rename-symbol`, `ca replace-symbol`, `ca move-symbol`, `ca delete-symbol` — not idempotent. Applying them twice produces a `symbol_not_found` error on the second call because the old name/location no longer exists.

### Concurrency

**v1 policy: one write op per project at a time.** Enforced by a repo-level file lock at `.ca-tools/write.lock`. Second concurrent call blocks until the first finishes (short timeout, then fails with `resource_busy`).

Reads are unaffected — always allowed, never blocked.

Rationale: agents aren't racing. If two agents edit the same project, they'll serialize naturally. Per-file locking is a future refinement if we see contention.

---

## Level 1: Protocols

The contracts the pipeline talks to. Each protocol is a Python `typing.Protocol` — structural typing, no forced inheritance, works with any object that satisfies the methods. Third parties can implement adapters without importing our base classes.

> **Rule: the pipeline and command resolvers never import language-specific modules (`ast`, `tree_sitter`, etc.) directly. They only call through protocol methods.** This is the single rule that makes the whole architecture work. If you catch yourself writing `import ast` outside an adapter implementation, stop — add a method to the protocol instead.

### `LanguageAdapter`

The base contract. Every language backend — native parser, tree-sitter, subprocess — implements this.

```python
from typing import Protocol, runtime_checkable
from pathlib import Path

@runtime_checkable
class LanguageAdapter(Protocol):
    """Syntactic parsing and enumeration for one language."""

    lang: str
    """Canonical language id (e.g. 'python', 'typescript', 'go'). Matches linguist output."""

    def symbols(self, path: Path, source: bytes) -> list[RawSymbol]:
        """Nested tree of symbols declared in this file."""

    def imports(self, path: Path, source: bytes) -> list[RawImport]:
        """All import statements."""

    def references_in(
        self, path: Path, source: bytes, symbol: RawSymbol
    ) -> list[RawRef]:
        """Name references inside a symbol's body. Textual-level, no resolution."""

    def validate_syntax(self, source: bytes) -> ParseResult:
        """Check whether bytes form a syntactically valid document."""

    def invalidate(self, path: Path) -> None:
        """Drop any cached state for this path. Called after writes."""
```

### `SemanticLanguageAdapter`

Adapters that additionally do binding resolution (partial or full). Extends `LanguageAdapter`; every semantic adapter is also a tier-1 adapter.

```python
@runtime_checkable
class SemanticLanguageAdapter(LanguageAdapter, Protocol):
    """Semantic analysis — name binding and reverse reference lookup."""

    def resolve_binding(
        self, path: Path, source: bytes, line: int, name: str
    ) -> BindingResolution:
        """Resolve `name` at `(path, line)` to the symbol it binds to.

        Returns structured result; may report `unsupported=True` for cases
        the adapter cannot handle (e.g. local scope resolution when the
        adapter only does module-scope binding). Callers surface the
        `reason` field in user-facing errors.
        """

    def references_to(
        self, symbol: SymbolPath
    ) -> ReferenceResult:
        """All references to a symbol, across the project.

        Like `resolve_binding`, may return `unsupported=True` for specific
        symbol kinds outside the adapter's competence.
        """
```

### Return types

All plain frozen dataclasses. Simple, hashable, serializable.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class RawSymbol:
    kind: str                     # per-language: "class" | "function" | "method" | ...
    name: str                     # leaf name
    qualified_path: str           # language-native dotted/scoped path
    byte_range: tuple[int, int]   # (start, end) — end exclusive
    line_range: tuple[int, int]   # (start, end) — both inclusive, 1-indexed
    signature_line: int           # line of def/class/etc. statement
    children: tuple["RawSymbol", ...] = ()   # nested: class contains methods

@dataclass(frozen=True)
class RawImport:
    line: int
    byte_range: tuple[int, int]
    statement: str                # raw text of the import line(s)
    imported_names: tuple[str, ...]
    module: str | None

@dataclass(frozen=True)
class RawRef:
    name: str
    line: int
    byte_offset: int              # absolute byte in file

@dataclass(frozen=True)
class ParseResult:
    ok: bool
    error_line: int | None = None
    error_message: str | None = None

@dataclass(frozen=True)
class BindingResolution:
    ok: bool
    symbol: "SymbolPath | None" = None
    reason: str | None = None
    unsupported: bool = False     # True = adapter cannot handle this case

@dataclass(frozen=True)
class ReferenceResult:
    ok: bool
    refs: tuple[RawRef, ...] = ()
    reason: str | None = None
    unsupported: bool = False

# SymbolPath is the language-native qualified path, identical format to RawSymbol.qualified_path
SymbolPath = str
```

### Design decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| ABC vs Protocol | **Protocol** | Structural typing; adapters wrapping external libs don't inherit from our base |
| Input shape | **`(path, source: bytes)`** | No filesystem access inside adapter; caller reads once |
| Tree object in signatures | **None** (no `ParseTree` leak) | Adapter caches internally; pipeline never touches tree objects |
| Caching location | **Per-adapter** | Tree types are adapter-specific; lifecycle varies by backend |
| Symbol output shape | **Nested** (external), **flat** (storage) | Adapter emits natural AST nesting; index flattens when storing |
| Kind vocabulary | **Per-language, no normalization** | Python has `async_function`; Go has `struct`; no fake universal taxonomy |
| Byte range convention | **End-exclusive** | Matches Python slice conventions and `ast.end_col_offset` |
| Capability enumeration | **None** — adapter fails gracefully with structured results | Avoids enum bloat; adapter knows its own limits best |
| Tier split | **Binary**: `LanguageAdapter` and `SemanticLanguageAdapter` | Two protocols, not three tiers. Specific cases may fail as `unsupported` |

### Semantic failure semantics

When a caller invokes a `SemanticLanguageAdapter` method and the adapter cannot handle a specific case (e.g. local scope resolution on our hand-built adapter), it returns:

```python
BindingResolution(
    ok=False,
    unsupported=True,
    reason="local scope resolution not supported by PythonAstAdapter (v1)"
)
```

The pipeline surfaces this as a user-facing error:

```json
{
  "status": "error",
  "error_code": "unsupported_operation",
  "message": "Semantic rename of local variables requires a backend with local scope resolution. PythonAstAdapter supports module-level names only. Use --tier textual or install a semantic backend (pyright).",
  "details": {
    "adapter": "PythonAstAdapter",
    "operation": "resolve_binding",
    "reason": "local scope resolution not supported by PythonAstAdapter (v1)"
  }
}
```

Add `unsupported_operation` to the error taxonomy.

### Registry

Adapters are registered by canonical language id (matching linguist's output):

```python
class LanguageRegistry:
    """Dispatches to the right adapter by language."""

    def tier1_adapter_for(self, path: Path) -> LanguageAdapter | None:
        """Return best available LanguageAdapter for this file, or None."""

    def semantic_adapter_for(self, path: Path) -> SemanticLanguageAdapter | None:
        """Return best available SemanticLanguageAdapter, or None."""
```

Resolution flow:
1. Caller passes a file path.
2. Registry consults linguist to detect language.
3. Looks up the language in its adapter tables.
4. Returns the adapter, or `None` if unsupported.

`None` → caller emits `unsupported_language` error.

### v1 adapter roster

Ships in v1:

| Adapter | Protocols | Language | Backend | Semantic scope |
|---|---|---|---|---|
| `PythonAstAdapter` | `LanguageAdapter`, `SemanticLanguageAdapter` | Python | stdlib `ast` + our own import/module analysis | Module-level names, import binding. `unsupported` for local scope, `self` attrs, closures |

Not in v1, slots reserved:

- `TreeSitterPythonAdapter` — tier-1 alternative for Python (cross-check / benchmark).
- `PyrightAdapter` / `JediAdapter` — full semantic for Python.
- `TreeSitterGoAdapter`, `GoplsAdapter` — Go.
- `TreeSitterTypeScriptAdapter`, `TSServerAdapter` — TypeScript.
- Additional tree-sitter tier-1 adapters for the long tail.

All of these plug in by implementing the protocols and registering. No pipeline or command changes.

### Migration plan for existing code

Current codebase imports `ast` in several places outside adapters. Migrate incrementally:

1. **Define protocols and return types** (this section).
2. **Implement `PythonAstAdapter`** wrapping current helpers. Same logic, different entry point — behavior cannot drift.
3. **Build writes against the adapter.** Writes never import `ast`.
4. **Migrate reads opportunistically** — when touching a checker/query for another reason, move it to the adapter. New read code always goes through the adapter.
5. **Remove direct `ast` usage** once all consumers are migrated.

During migration, `PythonAstAdapter` and direct `ast` usage coexist. The adapter must wrap the same helper functions the direct callers use, so there's only one implementation of the work and no drift.

---

## Next levels to define

This doc continues top-down:

- **Level 1: Protocols** — `LanguageAdapter`, `ReferenceResolver`, `WriteResolver`, `Validator`, `PostOpAnalyzer`. The interfaces that the pipeline talks to.
- **Level 2: Pipeline** — The 9-stage driver that composes protocols. Transaction manager. Registry wiring.
- **Level 3: Implementations** — `PythonAstAdapter`, `TextualResolver`, concrete validators and analyzers. What we actually build for v1.

Each level is defined in terms of the level above it. Implementations never leak upward.
