# Read cache — design notes

The read cache tracks what content we've served to an agent, so `ca patch` can apply edits without forcing a re-read. It's the safety rail that makes the write surface token-efficient.

Status: shipped. See `src/ca_tools/caches/` for implementations and `src/ca_tools/protocols/read_cache.py` for the contract.

## What the cache is for

Every `ca code` / `ca search` / `ca outline` call serves bytes to an agent. If that agent later calls `ca patch` on the same range, we need to know: did the agent actually see what's currently there?

- If yes → apply the patch.
- If no (never read, or file changed since) → `needs_read_confirmation` handshake.

Hashing on the agent side is theater (the agent can't verify what it's hashing). So we track served content ourselves.

## What we store

Cache entries are tiny — we store *what we served*, not the bytes themselves. Source bytes live on disk; we can re-slice anytime.

```
CacheEntry {
  file: str                  # repo-relative path
  start_byte: int
  end_byte: int
  served_hash: str           # short sha256 of the bytes we sent
  served_mtime: float        # file mtime at time of serve
  last_touched_at: float     # wall clock, for TTL
  last_tool_call_idx: int    # tool-call counter, for LRU
}
```

A session with hundreds of reads is still a few KB. Memory is not a concern.

## Primary deployment: MCP server

The MCP server is long-lived and session-aware. This is where the cache earns its keep.

```
On MCP client connect:     create fresh cache namespace
On MCP client disconnect:  drop namespace
On ca code/search serve:   upsert cache entry
On file mtime change:      invalidate or revalidate entries for that file
On every tool call:        increment counter, LRU-evict entries untouched for N calls
On idle timer:             drop namespace after 1h with no activity
```

Four eviction triggers, all cheap, all independent. They compose — no single one has to be right.

### Per-client scoping

Each MCP client gets its own cache namespace, keyed by MCP client ID. Two Claude Code instances don't contaminate each other.

### What MCP lets us detect

- **Session boundaries** — `initialize` and connection close are explicit protocol events. Cleanest signal we get.
- **Tool-call rhythm** — every tool call from the client increments our counter. Idle detection falls out for free.
- **File mtime** — cheap stat before every patch.

### What MCP does not give us

- **Compaction.** When Claude Code compacts conversation history, the MCP server has no notification. We don't need one. If compaction drops the agent's memory, its next patch hits `needs_read_confirmation`, which is already the fallback. Compaction is just one cause of "agent forgot."
- **"Agent is done with this file."** No signal. We approximate with tool-call-based LRU — if file X hasn't been touched in N calls, it's probably out of scope.

## Policy values to start with

| Setting | Initial value | Rationale |
|---|---|---|
| LRU threshold | 50 tool calls untouched | Adjust based on real session traces |
| Idle TTL | 1 hour | Conservative backstop; session close usually fires first |
| Per-file ranges cap | 200 | Large files get trimmed first |
| Per-session memory cap | ~5 MB | Well above realistic usage, safety net only |

Tune after we see real MCP sessions. None of these values is load-bearing.

## Non-MCP fallback: `CA_SESSION_ID`

Direct CLI usage (no MCP) has no session boundary. We fake one via env var:

```
export CA_SESSION_ID=$(uuidgen)
# all subsequent ca calls in this shell share cache
```

Cache persists to `.ca-tools/cache/sessions/<CA_SESSION_ID>.json` so the data survives across process invocations (each `ca` call is a fresh process). On session end, the user deletes the file or we garbage-collect on next run.

**Eviction policy** is the same as MCP: LRU on tool-call count, idle TTL on mtime-based last-touched. Session boundary is just the env var — when it changes or is absent, we start a fresh namespace.

### What if the user doesn't set it

Two options, both acceptable:

1. **No cache** — every patch triggers `needs_read_confirmation`. Safe, high friction. Correct for ad-hoc shell use where each command is isolated.
2. **Per-shell PID fallback** — key cache on `$PPID` or similar. Implicit session. Less reliable than explicit, but zero-config.

Start with option 1 (no cache without explicit session), revisit if friction is real.

MCP is the primary target. CLI-direct is expected to be rare enough that option 1's friction is acceptable.

## Cache lifecycle in detail

### On serve (`ca code` / `ca search` / `ca outline`)

```
1. Compute sha256 of bytes being sent.
2. Upsert CacheEntry(file, start, end, hash, mtime, now, tool_idx).
3. Bump session tool-call counter.
```

### On patch

```
1. Find overlapping cache entries for (file, range).
2. If none:         return needs_read_confirmation.
3. Stat file:
   - mtime unchanged → apply.
   - mtime changed:
     - Re-read target range, hash it.
     - If hash matches served_hash → silent refresh, apply.
     - If hash differs          → conflict (entry still valid info
                                   for what the agent last saw).
4. On apply success: invalidate cache entries that overlap the write
   (their byte ranges are now stale).
```

### On invalidation after a write

A patch shifts line numbers downstream. Cache entries in the same file *after* the patched range still have correct byte offsets if we tracked in bytes, but line mappings are off. Two approaches:

1. **Invalidate the whole file** on write. Simple, loses cache continuity.
2. **Invalidate only the patched range and anything overlapping it.** Keeps untouched ranges valid. Requires care because agent-facing responses use line numbers.

Start with option 1 (simple), optimize to 2 if chained edits in one file are common enough to matter.

### On MCP disconnect

Drop the whole namespace. Done.

### On idle timer

If no tool calls for 1 hour, drop the namespace. This catches clients that crashed without clean disconnect.

## Interaction with the symbol index

The symbol index is a separate concern (it's the structural AST data) and doesn't need this cache. The read cache is specifically about "what raw bytes did we serve."

That said, when `ca code <path>` resolves a symbol to a byte range via the index, the cache entry records the byte range — not the symbol identity. So if the same symbol is later asked for again but the file changed and its range moved, we don't falsely claim the agent has seen it.

## Open questions

- **Cache across git HEAD changes.** Checking out a different branch invalidates everything. Worth keying entries on `(file, mtime, git_sha)` when available? Leaning: mtime is enough; git operations change mtimes.
- **Streaming serves.** If we ever stream large responses, do we cache progressively or only on completion? Leaning: on completion only.
- **Cross-session reuse.** Never. Sessions are isolated on purpose — different agents, different trust boundaries.
- **Explicit flush API.** `ca cache clear` for the user to nuke state. Yes, eventually.
