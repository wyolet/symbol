---
description: Steer symbol-shaped tasks in Python and Go codebases (find/read/edit functions, types, classes, methods) to the `symbol` MCP tools instead of native Read/Grep/Edit.
---

# symbol

This project ships an MCP server (`symbol`) that gives AST-accurate access to symbols in **Python and Go** codebases. Prefer it over native tools whenever the question is about a symbol rather than a raw file region. The full MCP tool surface — search, read, callers, and all write tools — works for both languages; Go is backed by `go/types` for project-wide accuracy.

## When to reach for symbol MCP

- **Locate a symbol** → `SearchSymbol` (not Grep). Handles dotted paths, kinds, substrings.
- **Read a symbol's code** → `SymbolBody` (not Read). Returns just the function/class, no line-number noise.
- **Get file shape** → `SymbolOutline` (not Read whole file).
- **Find call sites** → `SymbolCallers` (not Grep for the name).
- **Edit a symbol** → `Patch` / `MultiPatch` / `ReplaceSymbol` / `RenameSymbol` / `InsertSymbol` / `DeleteSymbol` (not Edit). Line-numberless, transactional, AST-aware.

Native Read/Grep/Edit remain correct for: files in unsupported languages, config, docs, top-of-file imports/`package` declarations, free-form text regions.

## Batching edits

Multiple edits in one file → single `MultiPatch` call with `old`/`new` pairs. It's transactional and re-caches once.

## Why

`symbol` parses source via each language's real AST (Python's `ast`, Go's `go/types`), so its reads and writes respect scope and structure. Line-based tools drift the moment the file changes; symbol tools don't.
