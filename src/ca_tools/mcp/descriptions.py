"""Tool descriptions shown to the agent at tool-list time.

These are the first thing the model sees. They must earn the tool a
slot on every turn against the deeply-trained Read/Grep/Edit surface.
Every description leads with a concrete cost/capability claim, not an
abstract label.
"""

SEARCH_SYMBOL = """Narrow symbol candidates by name across the project. \
Returns qualified path, kind, location, and a short preview — no bodies.

Use instead of Grep when you want a symbol list. Grep scans raw lines \
of every file and returns unstructured matches; SearchSymbol resolves \
against a pre-built AST index and returns only real declarations with \
kind (class/function/method/async_function). Typical cost: 5-20× fewer \
tokens for a symbol exploration.

Multiple patterns AND together. Defaults to exact/dotted-suffix match; \
set regex or fixed for other match modes.
"""

SYMBOL_BODY = """Fetch the exact body of a symbol or line range. Returns \
body text + imports actually used + external refs.

Use instead of Read when you know the target. Read fetches the entire \
file (often thousands of tokens); SymbolBody returns just the declaration. \
Typical saving: 10× on files > 50 lines. Also records the range as \
"seen" so subsequent Patch calls on it skip needs_read_confirmation.

Address either as a qualified path (e.g. "services.user.UserService.save") \
or "file:start-end". If a qualified path is ambiguous, returns \
error_code="ambiguous" with candidate list — pass file:range instead.
"""

SYMBOL_OUTLINE = """Parent-child tree of symbols. Accepts either a file \
path (every top-level symbol in the file, children nested) or a symbol \
qualified path (the symbol plus all descendants).

Use instead of Read when you want structure without bodies. A file's \
outline is usually <5% of its content — the same shape Read delivers in \
raw form costs 20× more tokens.
"""

SYMBOL_CALLERS = """Find every symbol whose body references a given name. \
Tier-1 textual scan: matches the last name segment across all indexed refs.

Use instead of Grep when you want "who calls this". Grep returns lines \
matching the literal text; SymbolCallers returns the *containing symbol* \
for each ref, with file and line. Unresolved (name-match only), so a \
call to `other.save` matches when asking for `save` — confirm with \
SymbolBody on the containing symbol if disambiguation matters.
"""

PATCH = """Replace a byte range in a file with new content. The primitive \
edit — all other write operations compose from this.

Use instead of Edit when you know the target line range and want to send \
only the new content. Edit requires re-sending old content for \
disambiguation (~200 tokens per typical edit); Patch skips that round-trip.

Requires you've read the target range in this session (via SymbolBody or \
Read). If not, returns error_code="needs_read_confirmation" — fetch \
first, retry. Use force=true to skip the check (prefer not to).
"""

DELETE_SYMBOL = """Remove a symbol by qualified path. Atomic: parses the \
declaring file, validates no live callers (unless force=true), then splices.

Use instead of a manual Edit when removing a full class/function. \
Guaranteed to take the complete declaration with its decorators and docstring.

Returns error_code="has_live_references" with caller list if the symbol \
is in use. Pass force=true to delete anyway (caller code will break).
"""

INSERT_SYMBOL = """Insert code anchored to an existing symbol. Position: \
before | after | start | end (start/end operate inside a class body).

Use instead of Edit when adding a method, inserting a sibling function, \
or extending a class. Auto-indents to match anchor scope (set \
reindent=false to send content exactly as-is). Anchor is a qualified \
path resolved via the index.
"""

RENAME_SYMBOL = """Rename a symbol and update references across the \
project. Tier-1 textual: renames the declaration plus every textual \
reference matching the last name segment.

Use instead of a multi-file Edit. One transactional commit across all \
files; dry_run=true returns the plan without writing. If the project \
is under git, creates a checkpoint commit before writes — rollback via \
`git reset --hard HEAD^`.

Unresolved at tier-1: a rename of `save` also rewrites `other.save` if \
it appears in source. Use SymbolCallers first to preview the blast radius.
"""

REPLACE_SYMBOL = """Replace a symbol's full declaration. If the new \
content declares a different leaf name, callers are updated too \
(implicit rename included).

Use instead of Edit when rewriting a complete function or class. One \
transactional commit; dry_run=true returns the plan. Content must parse \
and contain exactly one top-level definition. If the agent sent content \
with leading indentation to match the target scope, it's dedented before \
parsing and re-indented before splicing.
"""
