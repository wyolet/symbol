---
name: ca-tools
description: Use when editing or navigating a Python codebase that has the ca-tools MCP server installed. Prefer AST-native tools (SearchSymbol, SymbolBody, SymbolOutline, SymbolCallers, Patch, DeleteSymbol, InsertSymbol, RenameSymbol, ReplaceSymbol) over Read/Grep/Edit for 5-20× token savings. If these tools aren't in your tool list the MCP server isn't running — fall back to native tools silently.
---

# ca-tools — AST-native agent operations for Python

ca-tools exposes a project's Python code through a pre-built symbol index: every class, function, method, and import addressable by qualified path (e.g. `services.user.UserService.save`). Its MCP tools route through that index instead of re-scanning raw file bytes. When both surfaces are available, these tools are strictly cheaper on any task that aligns to symbol boundaries.

## Default routing table

| You want to… | Use | Not |
|---|---|---|
| Find symbol declarations by name | **SearchSymbol** | Grep |
| Fetch a class/function body | **SymbolBody** | Read |
| See a file's structure | **SymbolOutline** | Read |
| Find "who calls X" | **SymbolCallers** | Grep |
| Replace a known line range | **Patch** | Edit |
| Rewrite a full function/class | **ReplaceSymbol** | Edit |
| Rename across files | **RenameSymbol** | multi-file Edit |
| Delete a full symbol | **DeleteSymbol** | Edit |
| Insert a new method/function | **InsertSymbol** | Edit |

## When to fall back to native tools

Use Read/Grep/Edit when:
- The change is in a docstring, comment, or non-symbol region (config dicts, module-level constants, string literals).
- You need regex across non-identifier content (TODO scans, raw text patterns).
- The file isn't indexed — non-Python, outside the project root, or brand new.
- You already have the exact content to replace and no symbol aligns — `Patch` by byte range still works, but a simple `Edit` may be clearer.

## Canonical read-then-edit workflow

```
1. SearchSymbol <name>           → shortlist with qualified paths
2. SymbolBody <qualified.path>   → exact body + refs + imports
   (also records the range as "seen" so Patch skips the read-confirmation check)
3. Decide the edit type:
   - Replace full definition        → ReplaceSymbol
   - Change a few lines inside it   → Patch (range already seen)
   - Remove it entirely             → DeleteSymbol
   - Add a sibling/method           → InsertSymbol
4. Before risky changes: SymbolCallers <name> to gauge blast radius
```

## Understanding error codes

The tools return `{ok: false, error_code, message}` on failure. Self-correct:

- **needs_read_confirmation** — you tried to Patch without reading. Call `SymbolBody` (or native `Read`) on the target first, then retry.
- **ambiguous** — a qualified path matched multiple symbols. The response has a `candidates` array with each candidate's `file:start-end`; pass one of those as the target instead.
- **symbol_not_found** — the qualified path doesn't exist in the index. `SearchSymbol` first to find the right one, or the index is stale (happens after external edits — rerun or restart the server).
- **has_live_references** — DeleteSymbol refused because callers exist. Either leave the symbol, update the callers first, or pass `force: true` to delete anyway.
- **parse_broken** — new content for ReplaceSymbol/InsertSymbol didn't parse. Fix the syntax and retry.
- **invalid_argument** — bad combination of flags (e.g. `regex` + `fixed` on SearchSymbol, unknown `position` on InsertSymbol).

## Addressing symbols

Qualified paths are repo-rooted dotted names following the Python module layout, with `src/` stripped:
- `ca_tools.shared.symbol_index.SymbolIndex.save` — a method
- `ca_tools.reads.search.search` — a free function
- `ca_tools.adapters.python_ast.PythonAstAdapter` — a class

For file-range addressing (`SymbolBody`, `Patch`), use repo-relative paths with line ranges:
- `src/ca_tools/mcp/server.py:120-145`

## Things these tools do NOT do

- No semantic/typed analysis (tier-1 textual only). `SymbolCallers save` will match `other.save` and any `.save` attribute access.
- No cross-language support v1 (Python only; TypeScript adapter is future work).
- No write-path parse validation inside Patch — that's `Patch`'s job to catch syntax breaks via your own retry, or use `ReplaceSymbol` which does validate.
- No file creation. Adding a new module still needs native `Write`.
