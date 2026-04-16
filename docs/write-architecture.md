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

### `rename`

**Semantic:** Change a symbol's name everywhere it appears in the codebase.

**Contract:**
- Input: `(old_qualified_path, new_name)`.
- Precondition: symbol exists in the index. No name collision at declaration scope. Working tree is clean (or `--allow-dirty`).
- Postcondition: the symbol's declaration and all detected references use the new name. The symbol's identity (kind, body, parameters) is unchanged — only the name differs.
- Invariant: the set of references updated depends on the resolution tier. Tier-1 (textual) may miss shadowed names or touch false positives. Tier-2 (semantic) is scope-correct.
- Safety: multi-file transaction with git checkpoint. Unresolved/ambiguous references are reported, not silently skipped or applied.

**Rename is the highest-value AST-aware write.** It replaces N file reads + N edits with one command.

### `move`

**Semantic:** Relocate a symbol from one file to another.

**Contract:**
- Input: `(qualified_path, new_qualified_path)`. File is derived from the qualified path.
- Precondition: symbol exists. No name collision in destination file. Working tree is clean.
- Postcondition: symbol exists in the destination file with identical body. Symbol is removed from the source file. Import statements across the codebase are NOT updated (v1 — agent drives import fix-up informed by post-op hints).
- Invariant: the symbol's body is byte-identical between source removal and destination insertion. No transformation applied.
- Safety: multi-file transaction (source + destination). Git checkpoint. Post-op analysis reports orphaned imports (source), unresolved names (destination), and stale callers.

**Move is relocation, not transformation.** Import surgery is explicitly out of scope for v1 and lives in post-op hints.

### `delete`

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
| Single-file | `patch`, `delete` (base), `insert-symbol` | Atomic per-file rename |
| Multi-file | `rename`, `move`, `delete --with-refs` | Two-phase commit + git checkpoint |

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

Operations that find references (`rename`, `delete --with-refs`) accept a tier:
- **Tier-1 (textual):** string match on qualified path + name. Fast, some false positives. Default.
- **Tier-2 (semantic):** scope-aware. Requires `LanguageAdapter.scope_of()`. Future.
- **Tier-3 (type-aware):** type-resolved references. Requires type info. Far future or LSP delegation.

The operation's response includes which tier was used. Higher tiers are opt-in (`--tier semantic`).

### Symbol kinds and safety

| Kind | rename | move | delete | insert-symbol |
|---|---|---|---|---|
| class | ✓ | ✓ | ✓ | ✓ (before/after/start/end) |
| function (module) | ✓ | ✓ | ✓ | ✓ (before/after) |
| method | ✓ | — | ✓ | ✓ (before/after within class) |
| constant (module) | ✓ | ✓ | ✓ | ✓ (before/after) |
| local variable | refuse (tier-1) | — | — | — |
| parameter | refuse (tier-1) | — | — | — |

Local/parameter rename requires `--scope file` or `--tier semantic`. This is a safety constraint, not a limitation.

---

## Next levels to define

This doc continues top-down:

- **Level 1: Protocols** — `LanguageAdapter`, `ReferenceResolver`, `WriteResolver`, `Validator`, `PostOpAnalyzer`. The interfaces that the pipeline talks to.
- **Level 2: Pipeline** — The 9-stage driver that composes protocols. Transaction manager. Registry wiring.
- **Level 3: Implementations** — `PythonAstAdapter`, `TextualResolver`, concrete validators and analyzers. What we actually build for v1.

Each level is defined in terms of the level above it. Implementations never leak upward.
