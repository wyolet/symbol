# Security Policy

## Reporting a vulnerability

Email **abror@aliboyev.com** with a description of the issue and (if possible) a reproducer. Please do not open a public GitHub issue for security-sensitive reports.

Expect an acknowledgement within 72 hours. Coordinated disclosure is appreciated for non-trivial issues.

## Scope

`symbol` performs **static analysis only** — it parses Python source with the `ast` module and never imports or executes target code. The most likely vulnerability classes are:

- Path-traversal or symlink escape when resolving project files
- Resource exhaustion (CPU / memory) on adversarial input files
- MCP write tools (`Patch`, `MultiPatch`, `InsertSymbol`, `DeleteSymbol`, `RenameSymbol`, `ReplaceSymbol`, `Undo`) modifying files outside the configured project root

Reports in these areas are especially welcome.

## Supported versions

Only the latest minor release on PyPI (`wyolet-symbol`) receives fixes during this 0.x stage.
