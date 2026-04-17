---
name: symbol
description: Use whenever the user asks about, edits, or navigates a Python symbol in this repo — questions like "how is X used", "who calls X", "where is X defined", "explain X", "rename X", "delete X", "rewrite X", "add a method to X", "refactor X". Triggers on intent, not file type. The MCP tools here are the FIRST choice for any work on indexed Python files; Grep/Read/Edit/Write are last-resort fallbacks for the narrow cases listed below.
---

# symbol — AST-native operations for Python

The MCP tools below are **the default tools** for any work on indexed Python files in this repo. Native Grep/Read/Edit/Write are **last-resort fallbacks** for a small set of cases listed at the bottom.

## Scenario → tool chain

The agent's task usually maps to one of these. Pick the chain by intent:

| What the user/task wants | Tool chain |
|---|---|
| "Where is X defined?" / "Find X" | **SearchSymbol(`X`)** |
| "Show me the code for X" | **SymbolBody(`<qualified.path>`)** |
| "Show me lines A-B of file" | **SymbolBody(`file:A-B`)** |
| "What's in this file?" / overview | **SymbolOutline(`file`)** → **SymbolBody(picked)** |
| "Who calls X?" / "Where is X used?" | **SymbolCallers(`X`)** |
| "Explain how X works" | **SearchSymbol(`X`)** → **SymbolBody(`<path>`)** → **SymbolCallers(`X`)** |
| "Change a few lines inside Y" | **SymbolBody(`Y`)** → **Patch(`file`, `range`, content)** |
| "Rewrite function/class X" | **ReplaceSymbol(`X`, content)** |
| "Add a method to class C" | **InsertSymbol(`C`, position=`end`, content)** |
| "Add a sibling function next to F" | **InsertSymbol(`F`, position=`after`, content)** |
| "Delete X" | **SymbolCallers(`X`)** (verify) → **DeleteSymbol(`X`)** |
| "Rename X to Y" | **SymbolCallers(`X`)** (preview) → **RenameSymbol(`X`, `Y`)** |
| "Refactor X to do Z" | **SymbolBody(`X`)** → **ReplaceSymbol(`X`, new content)** |
| "Add a new file at path P" | **Write(`P`, content)** ← only legitimate Write usage |

## Two roundtrips beat one — token math

The MCP tools require pairs (locate-then-fetch, read-then-edit). It feels like more calls. It is not — it is *less data per call*, and the totals come out lower:

- **SearchSymbol + SymbolBody** vs **Grep + Read**:
  Grep returns raw line matches mixed with comments, strings, unrelated identifiers. Read returns the entire file. The MCP pair returns only real declarations + only the relevant symbol's body with structural metadata. **Net: 5–20× fewer tokens** on typical "find X and show me" tasks, plus you get the symbol's used imports and refs for free.

- **SymbolBody + Patch** vs **Read + Edit**:
  Edit requires re-sending the existing content for disambiguation (~200 tokens per call). Patch addresses by line range — no `old_string` round-trip. The SymbolBody call also marks the range as "seen", satisfying Patch's read-confirmation check on the next call. **Net: ~200 tokens saved per write, plus byte-exact range targeting.**

- **SymbolOutline + SymbolBody** vs **Read** for "understand a file":
  Outline returns < 5% of the file's tokens. You see the structure, pick the relevant 1–2 symbols, fetch only those. **Net: typical 10–50× reduction** on "show me what's in this file."

These multipliers are the reason MCP wins on token cost despite needing more calls.

## The naming test

When in doubt, apply this single rule:

> **Can I name the thing I'm touching as a Python identifier or a known line range?**

- **Yes** → use the MCP tools above. Examples: `ASTCache`, `services.user.UserService.save`, `src/app.py:120-145`.
- **No** → native tools are correct. Examples: TODO scans, regex across comments, hunting a string literal, non-Python files.

This is the generative rule the scenario table derives from.

## What write tools uniquely guarantee (cannot be matched by Edit)

- **Patch** — byte-range replacement on an already-seen range. No `old_string` round-trip, no string-matching ambiguity.
- **ReplaceSymbol** — parses new content before committing; rejects syntax breaks. Rewrites callers automatically if the leaf name changes.
- **InsertSymbol** — places code by structural position (`before`/`after` a sibling, `start`/`end` of a class), auto-indented to scope.
- **RenameSymbol** — atomic across files, identifier-bounded (won't touch strings/comments), git-checkpointed (one-line undo).
- **DeleteSymbol** — refuses if callers exist (forces you to think). Returns the caller list so you can fix them first.

If your edit aligns to a symbol or a known range, one of these is the safe move. Edit's only correctness advantage is on *non-Python* files.

## When native tools win (last-resort fallbacks)

Use Grep / Read / Edit / Write only when one of these applies. There are not many cases:

- **Non-Python files** — markdown, TOML, JSON, shell scripts, YAML, etc. The index doesn't cover them; MCP tools won't help.
- **Text-pattern search across non-identifier content** — TODO/FIXME hunts, regex across comments and docstrings, hunting a specific string literal. Use Grep.
- **Genuinely needing the whole file as raw bytes** — top-level constants, module-level comments, or non-symbol regions when there's no way to bound them as a line range. Rare. Use Read.
- **Creating a new file** — Write. (For overwriting an existing indexed file, use ReplaceSymbol or Patch instead.)
- **Small Python files (< ~80 lines)** — the hook bypasses the nudge automatically; native tools are fine because MCP overhead exceeds the win at that size.

If you're reaching for a native tool for any other reason, you're probably bypassing rather than choosing — re-check against the scenario table above.

## Addressing symbols

Qualified paths are repo-rooted dotted names following the Python module layout, with `src/` stripped:
- `ca.symbol.shared.symbol_index.SymbolIndex.save` — a method
- `ca.symbol.reads.search.search` — a free function
- `ca.symbol.adapters.python_ast.PythonAstAdapter` — a class

For file-range addressing (`SymbolBody`, `Patch`), use repo-relative paths with line ranges:
- `src/ca/symbol/mcp/server.py:120-145`

## Error codes — self-correction

- **needs_read_confirmation** — Patch without a prior read of the range. Call `SymbolBody` (or native `Read`) first, then retry.
- **ambiguous** / **symbol_ambiguous** — qualified path matched multiple symbols. The response has a `candidates` array; pick one and pass its `file:start-end`.
- **symbol_not_found** — path not in the index. Run `SearchSymbol` to find the right path, or the index is stale.
- **has_live_references** — DeleteSymbol refused because callers exist. Update callers, or pass `force=true`.
- **parse_broken** — new content for ReplaceSymbol/InsertSymbol didn't parse. Fix syntax and retry.
- **name_collision** — RenameSymbol/ReplaceSymbol target name already exists as a sibling.
- **dirty_working_tree** — RenameSymbol/ReplaceSymbol refused because uncommitted changes block the checkpoint. Commit/stash, or pass `allow_dirty=true`.
- **invalid_argument** — bad flag combination (e.g. `regex` + `fixed`, `start`/`end` on a non-class anchor).

## What these tools do NOT do

- **Tier-1 textual only.** `SymbolCallers save` matches `other.save` and any `.save` attribute access — disambiguate with `SymbolBody`.
- **Python only** in v1. TypeScript adapter is future work.
- **No write-path parse validation in Patch.** Use `ReplaceSymbol` when you need that guarantee.
