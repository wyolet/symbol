"""Tool descriptions shown to the agent at tool-list time.

Each description leads with the user-task verb the agent will pattern-match
on (locate, fetch, show, who-uses, replace, etc.) — not with the tool's
internal mechanism. We deliberately avoid comparative claims against native
tools (Grep/Read/Edit) inside individual descriptions, because those make
the descriptions confusing when MCP is absent. Pair-level claims (like
"SymbolBody+Patch covers Read+Edit") live in the SKILL, not here.
"""

SEARCH_SYMBOL = """\
Locate where a class, function, method, or async function is declared by \
name. Returns a list of declarations with qualified path, kind, \
file:line-range, and a short non-docstring code preview. Bodies are not \
returned — chain SymbolBody for those.

USE WHEN the user mentions a symbol by name and wants to find, explore, \
rename, delete, or modify it. The standard two-call chain is \
SearchSymbol(name) → SymbolBody(qualified_path): the first narrows to real \
declarations, the second fetches the exact code with structural awareness. \
This pair is what an agent reaches for whenever it would otherwise grep \
for a name and read whatever file matches.

Parameters: patterns is a list of strings that AND together — every \
pattern must match the qualified path. By default, patterns match \
exactly or as a dotted suffix ("save" matches "UserService.save"). Set \
regex=true to interpret each pattern as a Python regex (unanchored \
re.search), or fixed=true for literal substring matches. kind filters \
by declaration kind ("class", "function", "method", "async_function"). \
file restricts to a single repo-relative path. limit caps results \
(default 100).

Returns declarations only — not bodies, not call graphs, not docstrings. \
For occurrences inside comments or string literals, this is the wrong tool.
"""

SYMBOL_BODY = """\
Get the exact source code of a function, class, method, OR an arbitrary \
line range in a file. Returns the source text, file:line-range, declared \
kind, the imports it actually uses, and the names it references.

Two address modes:
  • Qualified path ("services.user.UserService.save") — fetches one named symbol
  • file:start-end ("src/app.py:120-145") — any line range, raw slice

For named symbols, returns ONLY the declaration's lines (a 30-line method \
in a 2000-line module returns 30 lines + its imports). For the file:range \
mode, returns the exact slice — the same content a partial Read would, \
plus the structural metadata (refs, used imports) at no extra cost.

USE WHEN you know the symbol or line range you want. Pair with \
SearchSymbol when you know only the name. Pair with SymbolOutline when \
you want to preview a file's shape first. Pair with Patch for the \
read-then-edit workflow — SymbolBody marks the returned range as "seen" \
in the session's read-cache, which lets a subsequent Patch on the same \
range proceed without the needs_read_confirmation safety check.

Parameters: target is either a qualified path ("services.user.UserService.save") \
or a "file:start-end" address ("src/app.py:120-145"). Qualified paths \
are resolved via the index; line ranges are sliced from source bytes \
without re-parsing. include_refs defaults to true; set false to omit \
the refs list when you only need the body.

If a qualified path is ambiguous (multiple symbols match), returns \
error_code="ambiguous" with a candidates list — pick one and pass its \
file:range instead. Returns error_code="not_found" if nothing matches.
"""

SYMBOL_OUTLINE = """\
Show a file's symbols (classes, functions, methods, async functions) as \
a parent-child tree. Returns qualified paths, kinds, and line ranges — \
no bodies. A typical file's outline is < 5% of its content in tokens.

USE WHEN you want to understand the shape of a file before deciding \
which symbols to fetch. The standard chain is SymbolOutline(file) → \
SymbolBody(<one of the listed symbols>): see the structure, pick the \
relevant piece, get its exact code. This pair returns a fraction of what \
loading the entire file would.

Also accepts a symbol's qualified path — returns that symbol with all \
its descendants nested. Useful for previewing a class's methods before \
deciding which one to fetch.

Parameters: target is either (a) a repo-relative file path — returns \
every top-level symbol in that file with children nested, or (b) a \
symbol qualified path — returns the symbol plus all descendants. \
Dispatch is automatic: if target matches a known file or contains "/" \
or ends in a known source extension, it's treated as a file; otherwise \
as a symbol name.

Returns an empty roots list if the file isn't indexed or the symbol \
doesn't exist — no error code. For a best-effort symbol lookup that \
errors explicitly on miss, use SymbolBody.
"""

SYMBOL_CALLERS = """\
Find every indexed symbol whose body references a given name. For each \
hit, returns the source symbol's qualified path + file + line range, \
plus the matched ref's line and kind ("name" for a plain identifier \
use, "attr" for an attribute access like `x.save`).

USE WHEN you need to know who depends on a symbol — for blast-radius \
checks before renaming, deleting, or changing a signature, or for \
answering "who calls X / who uses X". Returns the *containing* symbol \
per call site, which is the unit you need to reason about impact (you \
don't care that line 47 references X; you care that `UserService.save` \
references X). Run this BEFORE any RenameSymbol, DeleteSymbol, or \
significant ReplaceSymbol on a leaf-name change.

Parameters: name is the identifier to look up. The lookup matches the \
last segment — a call to `other.save` matches when asking for "save", \
so results may include unrelated symbols that share a name. Disambiguate \
suspect hits with SymbolBody.

Returns an empty list (ok:true, count:0) when nothing references the \
name — not an error. Tier-1 textual: attribute accesses on typing \
annotations and string forward-refs are not captured.
"""

PATCH = """\
Replace a byte range in a file with new content. The primitive edit \
operation that all symbol-level writes compose from. Returns a unified \
diff, the before/after byte ranges, and line-count deltas.

USE WHEN you know the line range you want to change. Addresses by line \
range, so the old content does not need to be transmitted for \
disambiguation. The standard read-then-edit chain is SymbolBody(target) \
→ Patch(file, range, new_content): the SymbolBody call records the \
range as "seen", which satisfies Patch's needs_read_confirmation safety \
check on the next call. This pair handles ALL non-symbol edits — \
comments, constants, partial-function changes — and is the byte-exact \
write primitive for ranges you've already seen.

For whole-symbol rewrites, prefer ReplaceSymbol (parse-validated). For \
structural adds, prefer InsertSymbol (auto-indented).

Parameters: file is a path to the target. range is a line range string \
"A-B" (inclusive, 1-indexed). content is the replacement bytes — pass \
an empty string to delete the range. force=true skips the read-confirmation \
check (prefer not to use). dry_run=true computes the diff and returns it \
without writing the file.

Returns error_code="needs_read_confirmation" if the target range hasn't \
been seen this session. Other codes: "binary_file", \
"range_out_of_bounds", "file_not_found", "conflict" (file changed \
between preflight and write), "permission_denied". Patch does NOT \
validate that the result parses as Python — use ReplaceSymbol when you \
need that guarantee.
"""

DELETE_SYMBOL = """\
Remove a function, class, method, or async function by its qualified \
path. Splices out the complete declaration atomically — decorators, \
docstring, body, trailing blank line — without counting lines. Returns \
a unified diff.

USE WHEN you want to delete a named symbol cleanly. REFUSES BY DEFAULT \
when other code references the symbol — surfaces the caller list as an \
error so you can fix call sites first. Pass force=true to delete anyway \
(callers' code will break). The refusal is the safety net that makes \
this preferable to manual deletion.

Parameters: qualified_path is the symbol's full dotted path \
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
positioned by structural relationship, not line number. Returns a diff, \
the insertion line, and the anchor's qualified path.

Positions: "before" (sibling before the anchor), "after" (sibling \
after), "start" (first child of a class body), "end" (last child of a \
class body). Auto-indents the new content to match the anchor's scope, \
so you send code without leading whitespace. Preserves trailing-blank-line \
conventions typical of Python files.

USE WHEN adding a new method to a class, a sibling function next to an \
existing one, or any structural insertion. Skip line-number arithmetic \
— the anchor's qualified path plus a position keyword is enough.

Parameters: anchor is the qualified path of the reference symbol. \
position must be one of before/after/start/end (start/end only valid \
when anchor is a class). content is the new code — do not include \
leading indentation unless reindent=false. reindent=false passes \
content through untouched; use this only when you need exact control. \
dry_run=true returns the diff without writing.

Returns error_code="invalid_argument" on bad position or missing \
content, "symbol_not_found" or "symbol_ambiguous" on anchor resolution \
failures, "parse_broken" if your content has a syntax error. \
start/end on a non-class anchor returns "invalid_argument".
"""

RENAME_SYMBOL = """\
Rename a symbol's leaf identifier and rewrite every textual reference \
across the project in a single transaction. Under git, creates a \
checkpoint commit before any writes so the entire change reverts with \
`git reset --hard HEAD^`. Returns per-file change lists, total refs \
updated, and the declaring file.

USE WHEN renaming a class, function, or method anywhere it appears. \
Identifier-bounded — only updates references that lex as the bare \
identifier or as the trailing segment of an attribute access; stops at \
strings and comments. Atomicity + git checkpoint means no half-finished \
rename state is observable, and you have a one-line undo if the result \
is wrong.

Parameters: qualified_path is the current full path of the symbol. \
new_name is the new leaf name only (no dots). dry_run=true plans the \
rename and returns per_file deltas without writing. allow_dirty=true \
proceeds when the working tree has uncommitted changes (normally \
refused so the checkpoint commit has a clean base). force_no_vcs=true \
allows the rename on projects not under git, at the cost of no \
rollback path.

Tier-1 textual scope: a rename of "save" also rewrites any `x.save` \
where `x` is unrelated. Run SymbolCallers first to preview the blast \
radius. Returns "symbol_not_found", "symbol_ambiguous", "name_collision" \
(target name already exists as a sibling), or "dirty_working_tree" \
codes on failure.
"""

REPLACE_SYMBOL = """\
Replace a function or class with new content. If the new content \
declares a different leaf name, every caller is rewritten too (implicit \
rename included). Transactional under git like RenameSymbol: one \
checkpoint commit before writes. Returns per-file changes, name_changed \
flag, new_qualified_path, new_signature.

USE WHEN rewriting a complete function or class. Validates the new \
content parses as Python before committing — refuses on syntax errors. \
Send only the new definition; the old body is addressed via the index, \
not re-transmitted. The combination of parse-validation + atomic caller \
rewrite + git checkpoint makes this the safe primitive for whole-symbol \
rewrites.

New content must parse cleanly and contain exactly one top-level \
definition. Kind must match the replaced symbol (you cannot replace a \
function with a class).

Parameters: qualified_path is the current path of the symbol to \
replace. content is the full new definition — you may include the \
target's expected leading indentation (e.g. 4 spaces for a method); \
it's dedented before parsing and re-indented before splicing. \
dry_run=true previews the plan. allow_dirty and force_no_vcs behave \
as in RenameSymbol.

Returns "parse_broken" if the content has a syntax error, \
"invalid_argument" when content has zero or more than one top-level \
definition, "symbol_not_found" or "symbol_ambiguous" on lookup \
failures, "name_collision" when the new leaf name clashes with an \
existing sibling, "dirty_working_tree" if preconditions aren't met.
"""
