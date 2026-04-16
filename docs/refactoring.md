# Refactoring commands — design notes

AST-aware commands that mutate code across one or more files: `rename-symbol`, `replace-symbol`, `move-symbol`, `delete-symbol`, `insert-symbol`. All compose on top of `ca patch` (the universal write primitive — see `write-surface.md`) and share one transactional foundation.

**What we deliberately don't ship:** `extract`, `inline`, `signature`, and similar IDE-style refactors. These compose from `ca patch` calls driven by the agent — the hard part of each is generating the new code text, which the agent does better than any scope-analysis machinery we'd build. Keeping the surface small means fewer commands to learn and fewer ways to misuse them. The universal `patch` is the escape hatch.

Status: design only. Nothing implemented yet.

## Architectural placement

```
ca rename / move / delete / ...           ← refactoring commands (this doc)
            │
            ▼
   multi-file transaction layer           ← this doc
            │
            ▼
         ca patch                         ← single-file write engine
            │
            ▼
        read cache                        ← see read-cache.md
```

Refactoring commands are **resolvers + orchestrators**. They turn a high-level intent ("rename UserService to NewUserService") into a validated list of `patch` operations, then hand it to the transaction layer to commit atomically.

## Multi-file transactions

The transaction layer is the safety foundation for every cross-file op. It answers "how do we apply N patches across M files without corrupting the user's working tree?"

### Two-phase commit

**Phase 1 — Prepare (in memory, no side effects):**
```
for each patch in tx:
    resolve range
    verify range via read cache
    compute post-patch content
    parse-verify if Python
on any failure → abort, return full error report, nothing written
```

**Phase 2 — Commit (fast, mechanical):**
```
create git checkpoint commit
acquire per-file locks
for each file in tx:
    write content to file.tmp, fsync, atomic rename
release locks
re-index affected files
```

Phase 1 is pure computation. Phase 2 is deterministic and short. The split is what keeps corruption impossible: no write happens until every patch is proven valid.

### Git as the recovery layer

Multi-file ops require a git repository. Rationale: the only bulletproof undo for "agent made a mess across 12 files" is `git reset --hard`, and git gives it to us for free.

**Policy:**
- Working tree must be clean before multi-file ops. `--allow-dirty` overrides but warns.
- Phase 2 starts by creating a checkpoint commit: `ca-tools: pre-{op} {symbol}`.
- On Phase 2 failure, auto-roll-back via `git reset --hard <checkpoint>`.
- Success message includes the one-line undo: `Undo: git reset --hard HEAD~1`.
- Non-git projects: refuse multi-file ops unless `--force-no-vcs`. Single-file `patch` is unaffected — atomic per-file rename is sufficient.

**Checkpoint commits** are visible in history. Acceptable tradeoff: user can `git rebase -i` to squash them into real commits, or run `ca clean-checkpoints` (future helper). Bulletproof safety > clean history when an agent is driving.

### Intra-transaction ordering

Within a single file, multiple patches must be applied in reverse byte order so earlier edits don't shift later ranges. The transaction layer handles this automatically — refactoring commands produce an unordered patch set, transactions sort.

### Concurrency

v1 assumes no concurrent edits. Rationale: agents aren't racing. If the file changes between Phase 1 validation and Phase 2 write, we fail loudly and the user restarts.

Cross-file locking is punted until real contention shows up.

### Scope for v1

- Phase 1 / Phase 2 split, in-memory staging.
- Git checkpoint + auto-rollback.
- Require clean tree or `--allow-dirty`.
- Refuse non-git projects without `--force-no-vcs`.
- Atomic per-file rename.
- Parse-verify aborts Phase 1.
- Fail-loud on Phase 2 errors.

Deferred: cross-file locking, Phase 2 resume (`ca patch --resume <tx-id>`), partial-failure recovery beyond rollback.

---

## `ca rename-symbol`

Change every occurrence of a symbol and update its references.

### Tiers

**Tier-1 — textual (v1).** Match on qualified path + name using the symbol index and refs table. Fast, works across languages with any parser, but has false-positive risk on common names.

**Tier-2 — semantic (future).** Scope-aware resolution. Knows that `x` in function A is not the same `x` in function B. Required for variable renames and ambiguous method names.

v1 ships tier-1 only. Tier-2 happens when survey-corpus false-positive rate justifies the complexity.

### Kinds we support

| Kind | Tier-1 default | Notes |
|---|---|---|
| class | yes | Distinctive names, low collision |
| function (module-level) | yes | Usually safe |
| method | yes | Warn if name is shared across unrelated classes |
| module-level constant | yes | ALL_CAPS convention → low collision |
| local variable | **refuse** | Requires `--scope file` or `--tier 2` |
| parameter | **refuse** | Requires `--scope file` or `--tier 2` |
| attribute (instance) | yes with caveats | Warn on common names |

Refusing local-scope renames is a feature. Tier-1 on a local `x` would sweep the entire codebase — almost always wrong.

### Scope flags

```
ca rename <old> <new>              # whole project (default)
ca rename <old> <new> --scope file # only the declaring file
ca rename <old> <new> --scope dir  # only files under the declaring dir
```

File/dir scope is what makes local-variable rename safe.

### Response shape (text, default)

```
Renamed services.user.UserService → services.user.NewUserService
  class NewUserService(Base):   src/services/user.py:42

7 files changed, 23 refs updated
  src/api/handlers.py     4 refs @ 12, 45, 78, 91
  src/services/auth.py    3 refs @ 22, 103, 104
  src/services/user.py    9 refs @ 42, 67, 88, 142, 156, 170, 180, 201, 230
  ...

Unresolved: 0
Tier: textual
Undo: git reset --hard HEAD~1
```

For variables (no signature):
```
Renamed DEFAULT_TIMEOUT → DEFAULT_REQUEST_TIMEOUT
  DEFAULT_REQUEST_TIMEOUT = 30  # seconds   src/config.py:12
```

### Response shape (JSON, `--format json`)

```json
{
  "status": "applied",
  "symbol": {
    "kind": "class",
    "old_path": "services.user.UserService",
    "new_path": "services.user.NewUserService",
    "signature": "class NewUserService(Base):",
    "declared_at": "src/services/user.py:42"
  },
  "files_changed": 7,
  "references_updated": 23,
  "by_file": [
    { "file": "src/api/handlers.py", "refs": 4, "lines": [12, 45, 78, 91] }
  ],
  "unresolved": [],
  "tier": "tier-1-textual",
  "undo": "git reset --hard HEAD~1"
}
```

The response is **semantic, not textual** — no diff. Diffs are available via `git show HEAD` or `--verbose`.

Success criteria: an agent reading the response can (1) confirm the rename happened without reading files, (2) judge whether any unresolved entry needs follow-up, (3) see the new signature of the declaration site.

### Unresolved entries

When tier-1 is uncertain, it reports rather than silently acting. Two categories:

- **skipped** — we refused to touch (match in docs, comments, shadowed scope).
- **renamed but flagged** — we did it, but the match was in a string literal or other fuzzy context.

```json
"unresolved": [
  { "file": "README.md", "line": 42, "reason": "match in documentation", "action": "skipped" },
  { "file": "tests/test_user.py", "line": 5, "reason": "match inside string literal", "action": "renamed" }
]
```

### Output format

Text is the default for agent-facing responses — ~2-3x token savings vs JSON on large rename responses, and modern LLMs parse prose and JSON equally well. JSON is for MCP internals and CI scripts.

---

## `ca move-symbol`

Relocate a symbol from one file to another. v1 does not rewrite imports — it reports what will need to be fixed and lets the agent drive the cleanup.

### Guiding principle

> **The operation is clean. The telling is opinionated.**

Move does exactly the relocation. Protocol-level JSON is bare facts. Agent-facing text is enriched with next-step hints and post-op analysis.

### Argument shape

```
ca move <old-qualified-path> --to <new-qualified-path>
```

Example:
```
ca move services.user.UserService --to models.user.UserService
```

The destination is a **qualified symbol path**, not a file path. File is derived. This also lets you rename during move: `--to models.user.NewUserService`.

### What v1 does

1. Delete the symbol from its source file.
2. Insert the symbol at the end of the destination file (create the file if absent).
3. Report what moved, where from, where to.
4. In agent mode: run post-op analysis and emit hints.

### What v1 does NOT do

- **Rewrite import statements** in any file that referenced the old path.
- **Copy imports** from source to destination to support the moved symbol's dependencies.
- **Convert relative to absolute imports.**
- **Delete the source file** if it becomes empty.
- **Detect circular imports** introduced by the move.

All of this is the agent's job, informed by the response. Tradeoff: more tool calls post-move, but v1 ships in a tenth of the complexity. If real usage shows the manual cleanup is painful, add `--update-imports` as an opt-in flag later.

### Post-op analysis (agent mode only)

Uses the warm `ASTCache` — cheap, self-contained, no external tool dependencies.

**Source side:**
- **Orphaned imports** — imports in the remaining file with no references after the symbol's removal. Walk remaining AST, diff imports against name references.
- **Empty-file detection** — no top-level symbols left.

**Destination side:**
- **Unresolved names in the moved body** — names referenced by the moved symbol that aren't imported or defined in the destination. Walk the moved symbol's AST, collect free variables, check against destination's imports + top-level names.
- **Suggested imports** — for each unresolved name, look up how the source file imported it → suggest the same statement for destination.

Post-op checks are additive: they annotate the response, never block the move. Failing an analysis step does not fail the op.

**Why do this ourselves rather than suggest ruff:**
- Deterministic — ruff might not be installed, might be configured differently.
- Self-contained — we already own the AST.
- More specific — "X was imported to support UserService, now unused because UserService moved" beats "F401: X is unused."
- Consistent across projects regardless of their lint setup.

### Response shape (agent text)

```
Moved services.user.UserService → models.user.UserService
  Removed: src/services/user.py:42-89
  Inserted: src/models/user.py:15

⚠️  7 files still reference the old import path:
    Run: ca callers services.user.UserService

ℹ️  Source src/services/user.py — 3 imports now unused:
      from services.models import User
      from services.validators import validate_email
      from services.permissions import Permission
    Suggest: ca patch src/services/user.py --range <N>-<M> --with ""

ℹ️  Destination src/models/user.py — 3 names unresolved in moved body:
      User            (was: from services.models import User)
      validate_email  (was: from services.validators import validate_email)
      Permission      (was: from services.permissions import Permission)
    Suggest: ca patch src/models/user.py --range 1-1 --with "<imports>"

Undo: git reset --hard HEAD~1
```

### Response shape (JSON)

Bare facts, no analysis hints. Analysis lives in text-agent mode.

```json
{
  "status": "applied",
  "symbol": {
    "kind": "class",
    "old_path": "services.user.UserService",
    "new_path": "models.user.UserService"
  },
  "source": { "file": "src/services/user.py", "range": [42, 89] },
  "destination": { "file": "src/models/user.py", "line": 15, "created": false },
  "undo": "git reset --hard HEAD~1"
}
```

### Collisions and errors

- **Destination already has a symbol with that name** — refuse. Agent must choose a different name or delete first.
- **Source symbol not found** — refuse.
- **Destination file can't be created** (permission, parent dir missing) — refuse.
- **Moved symbol body fails parse-verify after insertion** — transaction rolls back via git checkpoint.

### Out of scope for v1

- `--after <anchor>` placement inside destination.
- Multi-symbol move (`ca move A,B,C --to ...`).
- Source file deletion when it becomes empty.
- Relative → absolute import conversion.
- Circular-import detection.
- Full tier-2 name resolution for unresolved-names analysis (we do tier-1-ish: module-level imports + top-level definitions in same file).

---

## Agent-mode and post-op analysis (cross-cutting)

Two orthogonal concepts that apply across all refactoring commands:

### `--agent` flag

Controls **rendering verbosity**, not output format.

|  | `--format text` (default) | `--format json` |
|---|---|---|
| no `--agent` | Rich for humans, minimal hints | minimal JSON |
| `--agent` | plain text with next-step hints, suggested commands, analysis findings | same minimal JSON |

JSON is always bare facts. Text gets hints when agent mode is on. MCP sets `--agent` implicitly on every call. Users can also `export CA_AGENT=1` or pass `--agent` directly.

Name: `--agent` (not `--mcp`) — broader, honest, covers any LLM-consuming pipeline.

### Post-op analysis framework

Every refactoring command has optional post-op checks that run in agent mode only. They:
- Use the warm `ASTCache` — no re-parse cost.
- Never block or fail the op.
- Annotate the response with actionable findings.

Per-command checks:

| Command | Source-side | Destination-side |
|---|---|---|
| `rename-symbol` | unresolved refs that look like they should've been renamed (textual near-misses) | — |
| `move-symbol` | orphaned imports, empty-file detection | unresolved names in moved body, suggested imports |
| `delete-symbol` | orphaned imports | — |
| `insert-symbol` | — | — (nothing to analyze) |

Post-op checks are additive. If a check fails to run (bug, unexpected AST), it's silently skipped. The op's correctness does not depend on them.

---

## `ca delete-symbol`

### v1 scope (shipping)

```
ca delete-symbol <qualified-path>
  --force     skip the caller check, delete anyway
  --dry-run   preview only
```

Single-file write. No cache check (symbol-level op — byte identity is not the contract; identity is the name). Uses `ca patch` engine underneath with empty content.

**Behavior:**
1. Resolve qualified path via index. Refuse if unresolved (`symbol_not_found`) or ambiguous (`symbol_ambiguous`).
2. `get_or_build_index` auto-refreshes staleness, so byte ranges are current.
3. Find callers via index's refs table.
4. If callers exist and no `--force` → refuse with `error_code: has_live_references` and list callers in response.
5. Otherwise → splice empty content over the symbol's byte range.
6. Report: deleted location, list of callers (which are now broken), pointer to `ca callers` / `ca patch` for fix-up.

Agent handles call-site cleanup. We delete the thing, we tell you what broke. That's the contract.

### Deferred: `--with-refs` and `--with-imports`

Not in v1. Listed here so the design isn't lost when we add them.

**Reference removal rules (when `--with-refs` lands):**
- `x = foo()` → replace with `x = None` (preserve assignment target).
- `foo()` alone on a line → remove the line.
- `return foo()` → replace with `return None` (preserve control flow).
- `f(foo())` inside an expression → **refuse / warn**; can't rewrite safely without type and position analysis.
- Attribute access inside expressions (`x.foo + 1`) → same, refuse.

**Import cleanup rules (when `--with-imports` lands):**
- Run *after* all ref removal.
- Check whether the imported name still appears anywhere in the file.
- If unused → remove the import line, preserving other names in grouped imports (`from x import a, b, c` where only `b` is now unused).
- If still used → leave alone.

**Strict order:**
1. Resolve all refs (requires tier-2 semantic resolution or our existing module-scope analysis).
2. Rewrite each call site per rules above.
3. Rewrite import statements per rules above.
4. Delete the declaration.
5. Parse-verify each file touched.
6. Refuse the whole transaction if any step can't be done cleanly — multi-file atomic via git checkpoint.

These come later, gated on better reference resolution. For now, agents chain patches.

---

## `ca insert-symbol`

Symbol-anchored insertion. Resolves an anchor symbol to a byte position via the index, calls `patch` with inferred indentation. One command, four positions.

```
ca insert-symbol --anchor <path> --position before|after|start|end --with <code>
```

### Positions

| `--position` | Where content lands | Typical use |
|---|---|---|
| `before` | Immediately above the anchor symbol | Add a helper function above a class |
| `after` | Immediately below the anchor symbol | Add a sibling method/function next to an anchor |
| `start` | Inside the anchor, at the top of its body | Add a docstring, or an early attribute |
| `end` | Inside the anchor, at the bottom of its body | Add a method at the end of a class, a statement at function end |

`start` and `end` are only valid for anchors with a body (classes, functions, methods). `before` and `after` work on any symbol.

### Indentation

Inferred from the anchor:
- `before` / `after` → match anchor's own indentation.
- `start` / `end` → anchor's indentation + one level.

Agent sends unindented content; we indent. (Agent can override with `--no-reindent` to send pre-indented content as-is.)

### Response

Same shape as other refactoring commands: semantic facts in JSON, enriched text in agent mode.

```
Inserted at src/services/user.py:56
  Position: after services.user.UserService.save
  Lines added: 8
Undo: git reset --hard HEAD~1
```

### Why one command, not four

All four positions resolve to a single `(file, byte_offset)` pair. The command's only real job is resolution. Splitting into `insert-before` / `insert-after` / `insert-at-start` / `insert-at-end` would quadruple the tool surface for zero added capability.

---

## Open questions (shared across refactoring commands)

- **Format preservation.** Run Black/Ruff on touched ranges automatically, or leave to user's hooks? Leaning: leave alone.
- **Checkpoint commit hygiene.** Do we squash checkpoint commits into the user's eventual commit, or leave them? Leaning: leave them, ship `ca clean-checkpoints` as a helper.
- **Tier-2 scope resolution.** When do we invest? Answer: when survey-corpus false-positive rate on real rename tasks crosses a threshold we haven't yet measured.
- **Multi-language writes.** Python first via `ast`. Tree-sitter for TS/Go/PHP later — writes are harder to get right cross-language than reads. Tier-1 textual rename works on any language now. IDE-style refactors (extract/inline/signature) are deliberately out of scope — composed from `patch` by the agent.
