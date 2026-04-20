---
description: Steer symbol-shaped tasks in Python codebases (find/read/edit functions, classes, methods) to the `symbol` MCP tools instead of native Read/Grep/Edit.
---

# symbol

This project ships an MCP server (`symbol`) that gives AST-accurate access to Python symbols. Prefer it over native tools whenever the question is about a symbol rather than a raw file region.

## When to reach for symbol MCP

- **Locate a symbol** → `SearchSymbol` (not Grep). Handles dotted paths, kinds, substrings.
- **Read a symbol's code** → `SymbolBody` (not Read). Returns just the function/class, no line-number noise.
- **Get file shape** → `SymbolOutline` (not Read whole file).
- **Find call sites** → `SymbolCallers` (not Grep for the name).
- **Edit a symbol** → `Patch` / `MultiPatch` / `ReplaceSymbol` / `RenameSymbol` / `InsertSymbol` / `DeleteSymbol` (not Edit). Line-numberless, transactional, AST-aware.

Native Read/Grep/Edit remain correct for: non-Python files, config, docs, top-of-file imports, free-form text regions.

## Batching edits

Multiple edits in one Python file → single `MultiPatch` call with `old`/`new` pairs. It's transactional and re-caches once.

## Why

`symbol` parses Python via AST, so its reads and writes respect scope and structure. Line-based tools drift the moment the file changes; symbol tools don't.
