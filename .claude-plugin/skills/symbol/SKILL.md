---
name: symbol
description: Use whenever the user asks about, edits, or navigates a Python symbol in this repo — questions like "how is X used", "who calls X", "where is X defined", "explain X", "rename X", "delete X", "rewrite X". Triggers on intent, not file type. If the MCP tools listed below aren't in your tool list, the server isn't running — fall back to native tools silently.
---

# symbol — AST-native operations for Python

## The naming test

Before reaching for Grep, Read, or Edit on a Python file, ask:

> **Can I name the thing I'm touching as an identifier or a known line range?**

- **Yes** → use the MCP tools below. Examples: `ASTCache`, `services.user.UserService.save`, `src/app.py:120-145`.
- **No** → native tools are correct. Examples: TODO scans, regex across comments, hunting a string literal, non-Python files.

This is the only rule. Every routing decision in this skill derives from it. You don't need to memorize phrasings — apply the test.

## The wrong first move (read)

User: *"explain how ASTCache is used"*

❌ `Grep "ASTCache"` then `Read ast_cache.py`
- Grep returns raw line matches in any file (comments, strings, unrelated names).
- Read dumps the whole file when you only needed the class.
- You miss callers that import the symbol but reference it through an alias or attribute.

✅ `SymbolCallers("ASTCache")` + `SymbolBody("ca.symbol.shared.ast_cache.ASTCache")`
- Returns every containing symbol that references the name, grouped by qualified path.
- Returns just the class body with its used imports and external refs.
- Two calls, complete picture, no file scan.

## The wrong first move (write)

User: *"rename ASTCache to ParseCache"*

❌ Multi-file `Edit` campaign
- Misses references in untracked files.
- Risks rewriting unrelated string literals containing the substring.
- No checkpoint to revert if the rename goes wrong.

✅ `RenameSymbol(qualified_path="...ASTCache", new_name="ParseCache")`
- Atomic across the project, identifier-bounded (won't touch strings/comments).
- Creates a git checkpoint commit first — full rollback with `git reset --hard HEAD^`.
- `dry_run=true` previews per-file deltas before committing.

## What the write tools uniquely guarantee

These aren't cheaper alternatives to `Edit` — they provide guarantees `Edit` cannot:

- **Patch** — byte-range replacement on an already-seen range. No re-sending old content for disambiguation.
- **ReplaceSymbol** — parses new content before committing; rejects syntax breaks. Rewrites callers automatically if the leaf name changes.
- **InsertSymbol** — places code by structural position (`before`/`after` a sibling, `start`/`end` of a class), auto-indented to scope. No line-number arithmetic.
- **RenameSymbol** — atomic, identifier-bounded, git-checkpointed. Multi-file Edit cannot match this.
- **DeleteSymbol** — refuses if callers exist (forces you to think). Returns the caller list so you can fix them first.

If your edit aligns to a symbol or a known range, one of these is the safe move. `Edit` is correct when the change crosses symbol boundaries or lives in non-symbol regions (comments, config dicts, strings).

## Canonical workflow

```
1. SearchSymbol <name>          → shortlist with qualified paths
2. SymbolBody <qualified.path>  → exact body + refs + used imports
   (the range is now "seen" — Patch on it skips read-confirmation)
3. Before risky changes:
   SymbolCallers <name>         → every containing symbol that references it
4. Pick the write tool by what's changing:
   - Few lines inside, range known → Patch
   - Whole definition rewritten     → ReplaceSymbol
   - Add a sibling/method           → InsertSymbol
   - Remove entirely                → DeleteSymbol
   - Rename across files            → RenameSymbol
```

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
- **No file creation.** Adding a new module still needs native `Write`.
- **No write-path parse validation in Patch.** Use `ReplaceSymbol` when you need that guarantee.

## Reference: routing table

| Task | Tool |
|---|---|
| Find symbol declarations by name | SearchSymbol |
| Fetch a class/function body | SymbolBody |
| See a file's structure | SymbolOutline |
| Find "who calls X" | SymbolCallers |
| Replace a known line range | Patch |
| Rewrite a full function/class | ReplaceSymbol |
| Rename across files | RenameSymbol |
| Delete a full symbol | DeleteSymbol |
| Insert a new method/function | InsertSymbol |
