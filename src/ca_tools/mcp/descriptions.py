"""Tool descriptions shown to the agent at tool-list time.

Follows Anthropic's tool-description guidelines: detailed plaintext (~4+
sentences) covering what the tool does, when to use it (and when not to),
what each parameter means, and important caveats. The description is the
single largest factor in whether the model picks this tool over a
deeply-trained native (Grep/Read/Edit), so it must earn its slot.
"""

SEARCH_SYMBOL = """\
Find symbol declarations (classes, functions, methods, async functions) \
by name across the indexed Python project. Returns a list of hits with \
qualified path, kind, file:line-range location, and a short non-docstring \
code preview. Bodies are not returned — use SymbolBody for that.

Use this instead of Grep whenever you want to locate a symbol. Grep \
returns raw lines matching the literal string anywhere in any file \
(including comments, docstrings, string literals, and variable names); \
SearchSymbol resolves against a pre-built AST index and returns only \
real declarations with their kind. This is typically 5-20× cheaper in \
tokens for the same task.

Parameters: patterns is a list of strings that AND together — every \
pattern must match the qualified path. By default, patterns match \
exactly or as a dotted suffix ("save" matches "UserService.save"). Set \
regex=true to interpret each pattern as a Python regex (unanchored \
re.search), or fixed=true for literal substring matches. kind filters \
by declaration kind ("class", "function", "method", "async_function"). \
file restricts to a single repo-relative path. limit caps results \
(default 100).

Does not return bodies, docstrings, or resolve call graphs. For those, \
follow up with SymbolBody on a hit. Does not search comments or string \
literals — use Grep for those.
"""

SYMBOL_BODY = """\
Retrieve the exact source body of a symbol or an arbitrary line range, \
with the symbol's external refs and the imports it actually uses. \
Returns body text + file:line-range + declared kind + used imports + \
per-ref list with kind ("name" or "attr") and line numbers.

Use this instead of Read whenever you already know the target symbol \
or line range. Read fetches the entire file — often thousands of tokens \
for a codebase where you only needed one function. SymbolBody returns \
only the declaration. Typical saving is 10× on files > 50 lines. It \
also records the range as "seen" in the session's read-cache, so a \
later Patch on the same range skips its needs_read_confirmation check.

Parameters: target is either a qualified path ("services.user.UserService.save") \
or a "file:start-end" address ("src/app.py:120-145"). Qualified paths \
are resolved via the index; line ranges are sliced from source bytes \
without re-parsing. include_refs defaults to true; set false to omit \
the refs list when you only need the body.

If a qualified path is ambiguous (multiple symbols match), returns \
error_code="ambiguous" with a candidates list — pick one and pass its \
file:range instead. Returns error_code="not_found" if nothing matches. \
Tier-1 textual: does not resolve imports semantically, only by declared \
usage inside the body.
"""

SYMBOL_OUTLINE = """\
Show a file's symbols or a symbol's descendants as a parent-child tree. \
Each node carries its qualified path, kind, line range, and children \
nested inside. Bodies are not returned; this is purely the structural \
shape.

Use this instead of Read when you want a file's structure without its \
content. A typical file's outline is < 5% of its content in tokens. \
Also use it instead of Read when you want to preview a class's methods \
before choosing which one to fetch with SymbolBody.

Parameters: target is either (a) a repo-relative file path — returns \
every top-level symbol in that file with children nested, or (b) a \
symbol qualified path — returns the symbol plus all descendants. \
Dispatch is automatic: if target matches a known file or contains "/" \
or ends in a known source extension, it's treated as a file; otherwise \
as a symbol name.

Returns an empty roots list if the file isn't indexed or the symbol \
doesn't exist — no error code. For a best-effort symbol lookup that \
errors explicitly on miss, use SymbolBody instead.
"""

SYMBOL_CALLERS = """\
Find every indexed symbol whose body references a given name. For each \
hit, returns the source symbol's qualified path + file + line, the \
matched ref's line inside the source symbol, and the ref kind ("name" \
for a plain identifier use, "attr" for an attribute access like \
`x.save`).

Use this instead of Grep for "who calls X" questions. Grep returns \
raw line matches without context; SymbolCallers returns the containing \
symbol for each call site, which is what you actually need to reason \
about blast radius before renaming, deleting, or changing signatures.

Parameters: name is the identifier to look up. The lookup matches the \
last segment — a call to `other.save` matches when asking for "save", \
so results may include unrelated symbols that happen to share a name. \
This is tier-1 textual: fast and cheap, but unresolved. Disambiguate \
by reading suspect hits with SymbolBody.

Returns an empty list (ok:true, count:0) when nothing references the \
name — not an error. Attribute accesses on typing annotations and \
string forward-refs are not captured.
"""

PATCH = """\
Replace a byte range in a file with new content. The primitive edit \
operation that all symbol-level writes compose from. Returns a unified \
diff, the before/after byte ranges, and line-count deltas.

Use this instead of Edit whenever you already know the target line \
range and just want to send the new content. Edit requires re-sending \
the existing content for disambiguation, typically ~200 extra tokens \
per edit; Patch skips that round-trip entirely. Best fit: files > 30 \
lines where re-transmitting old content is expensive. For whole-symbol \
rewrites prefer ReplaceSymbol; for structural adds prefer InsertSymbol.

Parameters: file is a path to the target. range is a line range string \
"A-B" (inclusive, 1-indexed). content is the replacement bytes — pass \
an empty string to delete the range. force=true skips the \
"have you read this?" safety check (prefer not to use). dry_run=true \
computes the diff and returns it without writing the file.

Returns error_code="needs_read_confirmation" if the target range hasn't \
been fetched this session via SymbolBody or Read; the remedy is to \
read first, then retry. Other error codes: "binary_file", \
"range_out_of_bounds", "file_not_found", "conflict" (file changed \
between preflight and write), "permission_denied". Patch does NOT \
validate that the result parses as Python — if you need that guarantee, \
use ReplaceSymbol.
"""

DELETE_SYMBOL = """\
Remove a full symbol (class, function, method, async function) by its \
qualified path. Atomic: the declaring file is identified via the index, \
the complete declaration with its decorators and body is spliced out, \
and a diff is returned.

Use this instead of manual Edit or Patch when removing a named symbol. \
It guarantees the full declaration is removed — decorators, docstring, \
body, and trailing blank line — without counting lines. It also checks \
for live references first, surfacing them as an error you can inspect \
before destroying working code.

Parameters: qualified_path is the symbol's full dotted path \
("services.user.UserService.save"). force=true proceeds even when \
callers exist — callers' code will break. dry_run=true returns the \
diff and caller list without writing.

Returns error_code="symbol_not_found" if the path doesn't resolve, \
"symbol_ambiguous" with candidate file:range list if multiple match, \
"has_live_references" with a callers array when force=false and refs \
exist. After a successful delete, rerun the index (or restart the MCP \
server) if you expect to query the removed symbol immediately — \
incremental refresh is usually automatic but not guaranteed.
"""

INSERT_SYMBOL = """\
Insert new code anchored to an existing symbol, at one of four \
positions: "before" (a sibling added before the anchor), "after" \
(sibling after), "start" (first child of a class body), or "end" \
(last child of a class body). Returns a diff, the insertion line, \
and the anchor's qualified path.

Use this instead of Edit when adding a method, inserting a sibling \
function, or extending a class. It resolves the anchor via the index \
and auto-indents the new content to match the anchor's scope, so you \
can send code without worrying about leading whitespace. It also \
preserves trailing-blank-line conventions typical of Python files.

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
across the project. Transactional: all files are updated in a single \
commit, and under git a checkpoint commit is created first so the \
entire change can be reverted with `git reset --hard HEAD^`. Returns \
per-file change lists, total refs updated, and the declaring file.

Use this instead of a multi-file Edit campaign when renaming anything. \
A manual rename often misses references in unrelated files or updates \
string literals that shouldn't change; RenameSymbol updates only \
identifier-bounded references and stops at string/comment boundaries. \
For preview-without-writing, pass dry_run=true — the per_file list \
tells you exactly what would change.

Parameters: qualified_path is the current full path of the symbol. \
new_name is the new leaf name only (no dots). dry_run=true plans the \
rename and returns per_file deltas without writing. allow_dirty=true \
proceeds when the working tree has uncommitted changes (normally \
refused so the checkpoint commit has a clean base). force_no_vcs=true \
allows the rename on projects not under git, at the cost of no \
rollback path.

Tier-1 textual scope: the rename updates every occurrence of the leaf \
name that appears to be an identifier reference. A rename of "save" \
also rewrites any `x.save` where `x` is unrelated. Run SymbolCallers \
first to preview the blast radius. Returns "symbol_not_found", \
"symbol_ambiguous", "name_collision" (target name already exists as a \
sibling), or "dirty_working_tree" codes on failure.
"""

REPLACE_SYMBOL = """\
Replace a symbol's full declaration with new content. If the new \
content declares a different leaf name, every caller is rewritten too \
(implicit rename included). Transactional under git like RenameSymbol: \
one checkpoint commit before writes. Returns per-file changes, \
name_changed flag, new_qualified_path, new_signature.

Use this instead of Edit when rewriting a complete function or class. \
A typical Edit requires re-sending the whole old body to disambiguate; \
ReplaceSymbol addresses the target via the index, so you only send the \
new definition. The new content must parse cleanly and contain exactly \
one top-level definition, which makes this safer than Edit for \
significant rewrites.

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
existing sibling, "dirty_working_tree" if preconditions aren't met. \
Kind must match the replaced symbol (you cannot replace a function \
with a class).
"""
