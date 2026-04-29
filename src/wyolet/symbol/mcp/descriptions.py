"""Tool descriptions shown to the agent at tool-list time.

Each description leads with the user-task verb the agent will pattern-match
on (locate, fetch, show, who-uses, replace, etc.). Descriptions state what
the tool expects and what it returns — not how it does it. Comparative
claims against native tools live in the SKILL, not here.
"""

SEARCH_SYMBOL = """\
Locate where a class, function, method, or async function is declared by \
name. Returns a list of declarations with qualified path, kind, \
file:line-range, and a short non-docstring code preview. Bodies are not \
returned — chain SymbolBody for those.

USE WHEN the user mentions a symbol by name and wants to find, explore, \
rename, delete, or modify it. The standard chain is SearchSymbol(name) → \
SymbolBody(target).

Parameters: patterns is a list of strings that AND together — every \
pattern must match the qualified path. By default, patterns match \
exactly or as a dotted suffix ("save" matches "UserService.save"). Set \
regex=true to use Python regex (unanchored re.search), or fixed=true \
for literal substring. kind filters by declaration kind ("class", \
"function", "method", "async_function"). file restricts to one \
repo-relative path. limit caps results (default 100).

Returns declarations only — not bodies, not call graphs, not docstrings. \
Response includes an `index_status` field: "ok" means the index is \
populated; "empty" means the index has zero symbols (likely a config or \
language-support issue) — treat a zero-count with index_status=empty as \
an infrastructure problem, not a "no such symbol" result.
"""

SYMBOL_BODY = """\
Get the exact source code of a function, class, method, OR an arbitrary \
line range in a file. Returns the source text, file:line-range, declared \
kind, and the imports the symbol actually uses (so you don't need to \
read the top of the file to know what's in scope).

Two address modes:
  • Qualified path ("services.user.UserService.save") — fetches one named symbol
  • file:start-end ("src/app.py:120-145") — any line range, raw slice

For named symbols, returns ONLY the declaration's lines (a 30-line \
method in a 2000-line module returns 30 lines). For file:range mode, \
returns the exact slice.

USE WHEN you know the symbol or line range you want. Pair with \
SearchSymbol when you know only the name; pair with SymbolOutline to \
preview a file's shape first. Also the read-then-edit primitive: a \
SymbolBody call marks the returned range as "seen" so a subsequent \
Patch on the same range skips the needs_read_confirmation check.

Parameters: target is a qualified path or "file:start-end". \
include_refs defaults to false; set true to also return the list of \
names referenced in the body. offset (default 0) and limit (default \
unlimited) paginate the body; total_lines is always reported, and a \
`window` field appears when the response is partial.

If a qualified path is ambiguous, returns error_code="ambiguous" with a \
candidates list. Returns "not_found" if nothing matches.
"""

SYMBOL_OUTLINE = """\
Show a file's symbols (classes, functions, methods, async functions) as \
a parent-child tree. Returns qualified paths, kinds, signatures, and \
line ranges — no bodies. A typical file's outline is < 5% of its \
content in tokens.

USE WHEN you want the shape of a file before deciding which symbols to \
fetch. The standard chain is SymbolOutline(file) → SymbolBody(one listed \
symbol).

Accepts either (a) a repo-relative file path — returns every top-level \
symbol with children nested, or (b) a symbol qualified path — returns \
that symbol plus all descendants. Dispatch is automatic based on the \
argument.

Leaf nodes (no children) omit the `children` field entirely — an \
absent key means "no children", not "unknown". Returns an empty roots \
list if the file isn't indexed or the symbol doesn't exist (no error \
code). For best-effort symbol lookup that errors explicitly on miss, \
use SymbolBody.
"""

SYMBOL_CALLERS = """\
Find every indexed symbol whose body references a given name. For each \
hit, returns the source symbol's qualified path + file + line range, \
plus the matched ref's line and kind ("name" for a plain identifier \
use, "attr" for an attribute access like `x.save`).

USE WHEN you need to know who depends on a symbol — for blast-radius \
checks before renaming, deleting, or changing a signature. Returns the \
*containing* symbol per call site, which is the unit you need to reason \
about impact. Run this BEFORE any RenameSymbol, DeleteSymbol, or \
signature-changing ReplaceSymbol.

Parameters: name is the identifier to look up. The lookup matches the \
last segment — a call to `other.save` matches when asking for "save", \
so results may include unrelated symbols that share a name. \
Disambiguate suspect hits by passing the hit's qualified path to \
SymbolBody and inspecting the body.

Returns an empty list (ok:true, count:0) when nothing references the \
name — not an error. Does NOT capture references inside string-form \
type annotations ('UserService'), `from __future__ import annotations` \
files, or comments and docstrings.
"""

PATCH = """\
Replace a byte range in a file with new content. The primitive edit \
operation that all symbol-level writes compose from. Returns the \
unified diff, the new content as it now appears at the patched range, \
the new line range (1-indexed), the before/after byte ranges, and \
line-count deltas.

USE WHEN you know the line range you want to change. Addresses by line \
range, so the old content does not need to be transmitted. The standard \
chain is SymbolBody(target) → Patch(file, range, new_content): the \
SymbolBody call marks the range as "seen", satisfying Patch's \
needs_read_confirmation safety check.

For whole-symbol rewrites, prefer ReplaceSymbol (parse-validated + \
caller rewrite). For structural adds, prefer InsertSymbol.

Parameters: file is a path to the target. range is a line range \
"A-B" (inclusive, 1-indexed). content is the replacement bytes — empty \
string deletes the range. force=true skips the read-confirmation check \
(prefer not to use). dry_run=true returns the diff without writing.

Returns error_code="needs_read_confirmation" if the target range \
hasn't been seen this session. Other codes: "binary_file", \
"range_out_of_bounds", "file_not_found", "conflict" (file changed \
between preflight and write), "permission_denied". Patch does NOT \
validate that the result parses — use ReplaceSymbol when you need that \
guarantee.
"""

MULTI_PATCH = """\
Apply N byte-range splices to one file atomically. Each edit is \
addressed by EITHER a line range OR an exact piece of old content. \
All edits succeed or none land; on success returns one merged unified \
diff.

**USE THIS OVER PATCH when**: you're batching multiple edits in one \
file, OR you have the exact old bytes for the change. Addressing by \
`old` skips the needs_read_confirmation check (sending the exact bytes \
is proof you know them) AND skips all line-number arithmetic, so \
earlier edits in the same batch don't shift later ones.

Choose `range` vs `old`:
  • Prefer `range` for a single edit where you just read the symbol.
  • Prefer `old` for batched edits in one file (line numbers would \
shift between edits), or when you already know the exact bytes.

Each edit is a dict: {content: "...", AND exactly one of \
range: "A-B" OR old: "exact bytes"}. Edits with `range` require prior \
SymbolBody coverage (or force=true); edits with `old` do not. Mixed \
batches with any unconfirmed range fail the whole call with \
status="needs_read_confirmation".

For `old` mode: the bytes must appear EXACTLY ONCE in the current \
file. Multiple matches return error_code="ambiguous" with line numbers \
— add context to narrow. No matches returns "not_found".

Parameters: file is a path to the target. edits is a list (each dict \
as above). force=true skips cache checks for range edits. dry_run=true \
returns the combined diff without writing.

Other error codes: "overlapping_edits" (two ranges intersect), \
"file_not_found", "permission_denied", "invalid_argument".
"""

DELETE_SYMBOL = """\
Remove a function, class, method, or async function by its qualified \
path. Splices out the complete declaration atomically — decorators, \
docstring, body — without counting lines. Returns a unified diff.

USE WHEN you want to delete a named symbol cleanly. REFUSES BY DEFAULT \
when other code references the symbol — surfaces the caller list as an \
error so you can fix call sites first. Pass force=true to delete anyway \
(callers' code will break).

Parameters: target is the full dotted path \
("services.user.UserService.save"). force=true proceeds even when \
callers exist. dry_run=true returns the diff and caller list without \
writing.

Returns error_code="symbol_not_found" if the path doesn't resolve, \
"symbol_ambiguous" with a candidate file:range list if multiple match, \
"has_live_references" with a callers array when force=false and refs \
exist.
"""

INSERT_SYMBOL = """\
Add a new function, method, or class anchored to an existing symbol — \
positioned by structural relationship, not line number. Returns a \
diff, the insertion line, and the anchor's qualified path.

Positions: "before" (sibling before the anchor), "after" (sibling \
after), "start" (first child of a class body), "end" (last child of a \
class body). start/end are only valid when the anchor is a class.

USE WHEN adding a new method to a class, a sibling function next to an \
existing one, or any structural insertion. No line-number arithmetic — \
the anchor's qualified path plus a position keyword is enough.

Parameters: target is the qualified path of the reference symbol. \
position must be one of before/after/start/end. content is the new \
definition source. dry_run=true returns the diff without writing.

Content must parse as Python and contain a single top-level \
definition. Returns error_code="parse_broken" on syntax errors, \
"invalid_argument" on bad position or missing content, \
"symbol_not_found" or "symbol_ambiguous" on anchor resolution failures.
"""

RENAME_SYMBOL = """\
Rename a symbol's leaf identifier and rewrite every textual reference \
across the project in one transaction. All-or-nothing: pre-images of \
every touched file are captured before writing, and any mid-write failure \
rolls back the entire change. Successful operations are recorded under \
.symbol/transactions/ and can be reverted with the Undo tool. Returns \
per-file change lists, total refs updated, the declaring file, and a \
transaction_id.

USE WHEN renaming a class, function, or method anywhere it appears. \
No git involvement — your git history stays clean.

Parameters: target is the current full path of the symbol. \
new_name is the new leaf name only (no dots). dry_run=true plans the \
rename and returns per_file deltas without writing.

Matches identifiers and the trailing segment of attribute accesses. A \
rename of "save" also rewrites unrelated `x.save` calls that share the \
leaf name. Run SymbolCallers first to preview the blast radius. Does \
NOT rewrite references inside strings, comments, or docstrings.

Returns "symbol_not_found", "symbol_ambiguous", "name_collision" \
(target name already exists as a sibling), or "write_failed" \
(rolled back) on failure.
"""

REPLACE_SYMBOL = """\
Replace a function or class with new content. If the new content \
declares a different leaf name, every caller is rewritten too (implicit \
rename included). Same transaction model as RenameSymbol: pre-image \
rollback on failure, persisted to .symbol/transactions/ for Undo. \
Returns per-file changes, name_changed flag, new_qualified_path, \
new_signature, transaction_id.

USE WHEN rewriting a complete function or class. Parse-validated — \
refuses on syntax errors before committing. Send only the new \
definition; the old body is addressed via the index.

Content must parse as Python and contain exactly one top-level \
definition. Kind must match the replaced symbol (you cannot replace a \
function with a class).

Parameters: target is the current path of the symbol. content is the \
full new definition. dry_run=true previews the plan.

Returns "parse_broken" on syntax errors, "invalid_argument" when \
content has zero or more than one top-level definition, \
"symbol_not_found" or "symbol_ambiguous" on lookup failures, \
"name_collision" when the new leaf name clashes with an existing \
sibling, "write_failed" (rolled back) on I/O failure.
"""
