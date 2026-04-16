"""Write command resolvers and preflight logic.

Each command (patch, rename-symbol, replace-symbol, move-symbol,
delete-symbol, insert-symbol) lives here. Resolvers are pure:
(args, context) -> preflight result or patch list. CLI wrappers in
`commands/` handle Typer and rendering.
"""
