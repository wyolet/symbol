# Write surface — design notes

Companion to the read-side symbol index (`ca search` / `ca code` / `ca outline` / `ca callers`). These are the AST-aware write commands that let agents mutate code efficiently.

Status: design only. Nothing implemented yet.

## Why write

Read side is done and measurably saves tokens on real tasks (fastapi, ca-tools). Writes are the next move because:

- Native LSP covers reads well enough. The read-surface war is mostly decided.
- Nobody is shipping an AST-native write layer for agents.
- Rename, move, and symbol-anchored insert are the operations agents burn the most tokens on today (N file reads + N edits + re-read to verify).

## Architectural principle

**One primitive, many resolvers.** `ca patch` is the single write engine. AST-based commands (`rename`, `delete`, `move`, `insert-after`, `extract`, etc.) are thin layers that resolve symbols to line/byte ranges and call `patch`.

Same discipline as the read side: `queries/` are pure functions returning data; `commands/` are thin views. For writes: `writes/` are resolvers returning `(file, range, content)` tuples; `patch` is the engine.

This means:
- Universal line/byte-range path works on any language today (no AST required).
- AST path is a convenience layer that buys token efficiency, not correctness.
- Validation, atomicity, reparse-verify, dry-run, and conflict handling live in one place.

## v1 command: `ca patch`

Patch has one uniform shape: **address + content**. Content determines the effect — replace, delete, or insert — no per-op flags.

```
ca patch FILE --range A-B --with CODE        # replace
ca patch FILE --range A-B --with ""          # delete (empty content)
ca patch FILE --range N-N --with CODE        # insert at line N (zero-width range)
ca patch FILE --confirm TOKEN                # confirm a pending patch
```

Content is passed via stdin or `--with -` for multiline. Dry-run is default; `--apply` commits.

### The three effects fall out of content

- **Replace** — range covers existing lines, content is new body.
- **Delete** — range covers existing lines, content is empty.
- **Insert** — range is zero-width (`N-N` means "between line N-1 and N"), content is the new block.

No `--insert-at` / `--delete` flags. One address, one content, one verb.

### Insert semantics

A zero-width range `N-N` means the content is placed *before* line N in the result. Line N and below shift down. Matches `sed i` and "insert line above."

Append to file: use `(EOF+1)-(EOF+1)`. Prepend: use `1-1`.

### Line ranges

Ranges are inclusive on both ends: `--range 120-145` replaces lines 120, 121, ..., 145. A zero-width insert range is the same start and end: `--range 120-120` (content goes before line 120).

## Read-cache as safety rail

Hashing on the agent side is theater — agents can't reliably hash content they didn't see. So the tool maintains its own read cache instead.

**Cache key:** `(file, mtime)`. **Cache value:** ranges we've served to the agent this session via `ca code` / `ca search` / `ca outline`, with their content hashes.

**On every patch:**
1. Does the target range fall inside a range we served this agent (same session, same mtime)?
2. If yes → apply.
3. If mtime changed but the target bytes are unchanged from what we served → apply silently.
4. If mtime changed and the target bytes differ → conflict.
5. If we never served this range → `needs_read_confirmation`.

**Cache persistence:** `.ca-tools/cache/reads.json`, keyed by file mtime. Survives process restarts so multi-step agent sessions don't lose context.

## The confirmation handshake

When the agent patches a range it didn't read, we don't reject — we return the current content plus a token:

```json
{
  "status": "needs_read_confirmation",
  "file": "src/services/user.py",
  "range": [120, 145],
  "current_content": "def save(self):\n    ...",
  "confirm_token": "ck_a3f9b2",
  "expires_in": "60s"
}
```

Agent either:
- **Confirms** via `ca patch --confirm ck_a3f9b2` → we apply.
- **Updates** by sending a new patch with the now-seen content.

**Why a token, not a semantic "do you agree":** the handshake is mechanical. Weak models that rubber-stamp the confirm still win, because the current content was in their context window when they made the decision. Exposure is the contract, not judgment.

**Why this beats forced re-read:** the current content is right there in the response — no second `read` tool call. Saves a round trip and keeps the original patch body in play if it's still correct.

## Response contract

Every patch call returns structured JSON:

```json
{
  "status": "applied" | "needs_read_confirmation" | "conflict" | "invalid",
  "file": "src/services/user.py",
  "before": { "range": [120, 145], "hash": "a3f9b2" },
  "after":  { "range": [120, 162], "hash": "7c12de" },
  "diff": "--- a/...\n+++ b/...\n@@ ...",
  "reparse": "ok" | "broken" | "skipped",
  "lines_added": 17,
  "lines_removed": 0
}
```

Why each field matters:

- **`before` / `after` ranges** — chained edits need updated line numbers. Patch #2 would be wrong without this.
- **`diff`** — source of truth for what actually changed. Agent can show it or verify.
- **`reparse`** — crash guard, not correctness guard. Python-only for v1.
- **`lines_added` / `lines_removed`** — helps the agent maintain a mental model of file length.

On `conflict`, additionally:
- **`staged_at`** — path where the proposed content was saved (`.ca-tools/staging/patch-<id>.py`). Recovery via `ca patch --apply-staged <id>` after manual inspection.
- **`current_content`** — what's actually there now.
- **`reason`** — one-line explanation.

On `needs_read_confirmation`:
- **`current_content`** — the range as it exists now.
- **`confirm_token`** — opaque, short-lived.
- **`expires_in`** — cleanup hint.

## Scenarios `patch` must cover

### By operation
1. Replace — range + content
2. Delete — range, no content
3. Insert — anchor line + content (zero-width replace)
4. Append to file — anchor = EOF
5. Prepend — anchor = line 0

### By read-cache state
6. Agent read the range this session, file unchanged → apply.
7. Agent read, mtime changed, target bytes unchanged → apply silently.
8. Agent read, mtime changed, target bytes differ → conflict.
9. Agent never read this range → `needs_read_confirmation`.
10. Agent read adjacent but not overlapping range → `needs_read_confirmation`.
11. Confirm token expired → reissue.

### By multi-edit chains
12. Second patch, lines shifted by earlier patch → agent uses new numbers from patch #1's response.
13. Second patch on range we just wrote → auto-trusted (served in last response).
14. Second patch on range invalidated by shift → `needs_read_confirmation` for the shifted zone.

### By validation outcome
15. Range valid, new content parses → apply, reparse ok.
16. Range valid, new content breaks parse → roll back, return error unless `--allow-broken`.
17. Range out of bounds → reject with current file length.
18. Range crosses a symbol boundary mid-body → allow, flag in response.
19. Insert-at line beyond EOF → treat as append, note in response.

### By filesystem
20. File doesn't exist → reject (use `ca create` — separate op).
21. File is binary / non-UTF-8 → reject.
22. File is a symlink → follow, note in response.
23. Permission denied → reject with clear error.
24. File changed between confirm issue and apply → invalidate token, reissue.
25. Concurrent patches on same file → serialize via file lock.

### By mode
26. `--dry-run` (default) → return diff, don't write.
27. `--apply` → write, return diff + post-state.
28. `--apply` but conflict → don't write, return conflict payload.

### By response status
29. `applied` — before/after/diff/reparse.
30. `needs_read_confirmation` — current content + token + expiry.
31. `conflict` — staged path + current content + reason.
32. `invalid` — reason, no side effects.

### Internal state machine

```
resolve range → validate FS → check read-cache → [apply | confirm | conflict] → [parse-verify | rollback] → respond
```

## v1 scope

Ship `patch` covering scenarios: 1–5, 6, 9, 15, 26–32.

Deferred to follow-up tickets:
- Silent refresh on mtime change when bytes match (7)
- Full conflict staging/recovery (8, 14)
- Chained-edit invalidation tracking (12–14)
- Parse-verify and rollback polish (16, `--allow-broken`)
- Symlink, binary, permission edge cases (20–23)
- Concurrency lock (25)

## AST-based commands (future)

Once `patch` is solid, these become trivial resolvers:

- **`ca rename <old> <new>`** — index → N refs → N `patch` calls in a transaction. Tier-1 textual first; tier-2 semantic later behind `--strict`.
- **`ca delete <path>`** — index → 1 range → 1 `patch --delete`. `--with-refs` for cascading.
- **`ca insert-after <path>`** — index → anchor line → 1 `patch --insert-at`. Indentation inferred from the anchor symbol.
- **`ca move <path> <new-file>`** — compose: delete at source + insert at dest + update imports.
- **`ca extract <file:range> <name>`** — scope analysis → new function → `patch` original block with call site.
- **`ca inline <path>`** — inverse of extract.
- **`ca signature <path> <new-sig>`** — change declaration + optionally update callers.

These all depend on the symbol index plus resolvers returning ranges. Rename is the headline feature (biggest token savings vs raw Edit).

## Imports get their own command

Adding an import is a line-insert op, not a body replacement. Frequent, small, doesn't need symbol-anchor machinery.

```
ca add-import <file> <statement>          # idempotent; no-op if already present
ca remove-import <file> <name>
```

Written on top of `patch` internally, but exposed as a convenience because import handling is annoying enough to deserve a dedicated surface.

## MCP exposure

`ca mcp serve` exposes reads + writes as MCP tools. Reads are trivial. Writes need all the safety rails above *plus*:

- Writes behind an approval role, not auto-invoked.
- Each write tool returns the diff before asking for confirmation.
- The read-cache becomes per-session scoped to the MCP client.

## Shipping order

1. **`ca patch`** — core scenarios (1–5, 6, 9, 15, 26–32). Language-agnostic. This is the foundation.
2. **`ca add-import` / `ca remove-import`** — thin convenience on top of `patch`. High-value, low risk.
3. **`ca rename`** — first AST-based write. Tier-1 textual. Biggest token-savings demo vs raw Edit.
4. **`ca delete` + `ca insert-after`** — symbol-anchored wrappers around `patch`.
5. **Fill out `patch` edge cases** — conflict staging, chained-edit tracking, parse-verify polish.
6. **`ca move`** — first cross-file AST op.
7. **`ca extract` / `ca inline` / `ca signature`** — advanced refactors. Python only for v1.

## Open questions

- **Format preservation** — run Black/Ruff on touched ranges automatically, or leave formatting to the user's hooks? Leaning: leave alone, let pre-commit handle it.
- **Undo journal** — write a `.ca-tools/undo/` log per write, or trust git? Leaning: trust git, document the assumption.
- **Rename: tier-1 vs tier-2 refs** — start textual, measure false-positive rate on the survey corpus, decide whether scope-aware resolution is worth the complexity. Handled as a separate design round.
- **Multi-language writes** — Python first. Tree-sitter for TS/Go/PHP is a later ticket; writes are harder to get right cross-language than reads.
- **Confirm token storage** — in-memory only (session-scoped) vs on-disk. Leaning: in-memory is enough; agents that lose process context can just resend and get a new token.
