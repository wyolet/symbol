"""Tool descriptions shown to the agent at tool-list time.

Each description states what the tool uniquely does and guarantees. We
deliberately avoid comparative claims against native tools (Grep/Read/Edit) —
those tools may not be present, and the comparison creates noise when they
are. Routing decisions live in the ca-tools skill, not in tool descriptions.
"""

SEARCH_SYMBOL = """\
Find symbol declarations (classes, functions, methods, async functions) \
by name across the indexed Python project. Returns a list of hits with \
qualified path, kind, file:line-range location, and a short non-docstring \
code preview. Bodies are not returned — follow up with SymbolBody for those.

Resolves against a pre-built AST index, so results are always real \
declarations with their kind. Comments, docstrings, string literals, and \
variable references are never matched. The qualified path returned is the \
canonical address you pass to SymbolBody, SymbolCallers, RenameSymbol, \
DeleteSymbol, and ReplaceSymbol.

Parameters: patterns is a list of strings that AND together — every \
pattern must match the qualified path. By default, patterns match \
exactly or as a dotted suffix ("save" matches "UserService.save"). Set \
regex=true to interpret each pattern as a Python regex (unanchored \
re.search), or fixed=true for literal substring matches. kind filters \
by declaration kind ("class", "function", "method", "async_function"). \
file restricts to a single repo-relative path. limit caps results \
(default 100).

Returns declarations only — not bodies, not call graphs, not docstrings. \
For occurrences inside comments or string literals, this tool will not \
find them; that is a different kind of search.
"""

SYMBOL_BODY = """\
Retrieve the exact source body of a symbol or an arbitrary line range, \
plus the symbol's external refs and the imports it actually uses. Returns \
body text, file:line-range, declared kind, used imports, and a per-ref \
list with kind ("name" or "attr") and line numbers.

Scoped to the declaration, not the file: a 30-line method in a 2000-line \
module returns only those 30 lines and the imports they touch. Calling \
this also records the returned range as "seen" in the session's \
read-cache, which lets a subsequent Patch on the same range proceed \
without the needs_read_confirmation safety check.

Parameters: target is either a qualified path ("services.user.UserService.save") \
or a "file:start-end" address ("src/app.py:120-145"). Qualified paths \
are resolved via the index; line ranges are sliced from source bytes \
without re-parsing. include_refs defaults to true; set false to omit \
the refs list when you only need the body.

If a qualified path is ambiguous (multiple symbols match), returns \
error_code="ambiguous" with a candidates list — pick one and pass its \
file:range instead. Returns error_code="not_found" if nothing matches. \
Tier-1 textual: refs are listed by declared usage inside the body, not \
by import resolution.
"""

SYMBOL_OUTLINE = """\
Show a file's symbols or a symbol's descendants as a parent-child tree. \
Each node carries its qualified path, kind, line range, and children \
nested inside. Bodies are not returned; this is purely the structural \
shape.

Useful for previewing a class's methods or a module's top-level shape \
before deciding which body to fetch. Pairs with SymbolBody: outline to \
locate, SymbolBody to inspect.

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
hit, returns the source symbol's qualified path, file, and line range, \
plus the matched ref's line and kind ("name" for a plain identifier, \
"attr" for an attribute access like `x.save`).

Returns the containing symbol per call site, not just matching lines — \
which is the unit you need for blast-radius reasoning before renaming, \
deleting, or changing a signature. Run this before any RenameSymbol, \
DeleteSymbol, or ReplaceSymbol that changes a leaf name.

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

Addresses by line range, so no old content needs to be transmitted for \
disambiguation. Includes a needs_read_confirmation safety check: the \
target range must have been fetched this session via SymbolBody (or a \
native read) before Patch will write. For whole-symbol rewrites prefer \
ReplaceSymbol (which validates parse); for structural adds prefer \
InsertSymbol (which auto-indents).

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
Remove a full symbol (class, function, method, async function) by its \
qualified path. The declaring file is identified via the index, the \
complete declaration — decorators, docstring, body, trailing blank line \
— is spliced out atomically, and a diff is returned.

Refuses by default if live references exist: returns the caller list as \
an error so you can fix call sites before destroying working code. Pass \
force=true to delete anyway.

Parameters: qualified_path is the symbol's full dotted path \
("services.user.UserService.save"). force=true proceeds even when \
callers exist — callers' code will break. dry_run=true returns the \
diff and caller list without writing.

Returns error_code="symbol_not_found" if the path doesn't resolve, \
"symbol_ambiguous" with a candidate file:range list if multiple match, \
"has_live_references" with a callers array when force=false and refs \
exist. After a successful delete, the index refreshes incrementally; \
re-query with SearchSymbol if a follow-up call seems stale.
"""

INSERT_SYMBOL = """\
Insert new code anchored to an existing symbol, at one of four \
positions: "before" (a sibling added before the anchor), "after" \
(sibling after), "start" (first child of a class body), or "end" \
(last child of a class body). Returns a diff, the insertion line, \
and the anchor's qualified path.

Positions by structural relationship — sibling-of or child-of a named \
symbol — never by raw line number. Auto-indents the new content to \
match the anchor's scope, so you send code without leading whitespace. \
Preserves trailing-blank-line conventions typical of Python files.

Parameters: anchor is the qualified path of the reference symbol. \
position must be one of before/after/start/end (start/end only valid \
when anchor is a class). content is the new code — do not include \
leading indentation unless reindent=false. reindent=false passes \
content through untouched; use this only when you need exact control. \
dry_run=true returns the diff without writing.

Returns error_code="invalid_argument" on bad position or missing \
content, "symbol_not_found" or "symbol_ambiguous" on anchor resolution \
failures, "parse_broken" if your content has a syntax error. \
start/end on a non-class anchor returns "invalid_argument" — use \
before/after for function anchors.
"""

RENAME_SYMBOL = """\
Rename a symbol's leaf identifier and rewrite every textual reference \
across the project in a single transaction. Under git, a checkpoint \
commit is created before any writes so the entire change can be reverted \
with `git reset --hard HEAD^`. Returns per-file change lists, total \
refs updated, and the declaring file.

Identifier-bounded: only updates references that lex as the bare \
identifier or as the trailing segment of an attribute access. Stops at \
string and comment boundaries. Atomicity + git checkpoint means no \
half-finished rename state is observable.

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
Replace a symbol's full declaration with new content. If the new \
content declares a different leaf name, every caller is rewritten too \
(implicit rename included). Transactional under git like RenameSymbol: \
one checkpoint commit before writes. Returns per-file changes, \
name_changed flag, new_qualified_path, new_signature.

Validates the replacement: content must parse cleanly as Python and \
contain exactly one top-level definition. Kind must match the replaced \
symbol — you cannot replace a function with a class. The combination \
of parse-validation + atomic caller rewrite + git checkpoint makes this \
the safe primitive for whole-definition rewrites.

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
