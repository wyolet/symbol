# Ops dataset spec — differential test suite

You are generating a test dataset for `scripts/diff_runner.py`, which runs
every op against TWO implementations of symbol (Python reference and the
Rust port) and diffs the results. Your job: given a target repository,
produce a JSONL file of operations with high coverage of edge cases. You do
NOT write expected outputs — the Python implementation is the oracle; the
runner compares the two implementations against each other.

## Format

One JSON object per line:

```jsonl
{"id": "r001", "op": "code", "args": {"target": "app.models.UserService.save"}}
{"id": "r002", "op": "code", "args": {"target": "app/models.py:19-20"}}
{"id": "r003", "op": "callers", "args": {"name": "save"}}
{"id": "r004", "op": "outline", "args": {"query": "app/models.py"}}
{"id": "r005", "op": "outline", "args": {"query": "UserService"}}
{"id": "r006", "op": "search", "args": {"patterns": ["save"], "kind": "method", "limit": 50}}
{"id": "r007", "op": "search", "args": {"patterns": ["user", "service"], "fixed": true, "ignore_case": true}}
{"id": "w001", "op": "rename-symbol", "args": {"qualified_path": "app.models.make_service", "new_name": "build_service"}, "tags": ["engine-kind"]}
{"id": "w002", "op": "replace-symbol", "args": {"qualified_path": "app.models.UserService.load", "content": "def load(self, user_id):\n    return self.db.read(user_id)\n"}, "tags": ["engine-kind"]}
{"id": "w003", "op": "delete-symbol", "args": {"qualified_path": "app.models.UserService.load"}, "expect_error": true}
{"id": "w004", "op": "delete-symbol", "args": {"qualified_path": "app.orphan.unused_helper", "force": true}}
{"id": "w005", "op": "patch", "args": {"file": "app/models.py", "range": "1-1", "content": "\"\"\"Data models.\"\"\""}}
{"id": "w006", "op": "replace-symbol", "args": {"qualified_path": "app.models.UserService.save", "content": "def save(self, row):\n    return self.db.write(row)\n"}}
{"id": "w007", "op": "insert-symbol", "args": {"anchor_path": "app.models.UserService.save", "position": "after", "content": "def touch(self):\n    pass\n"}}
{"id": "w008", "op": "insert-symbol", "args": {"anchor_path": "app.models.UserService", "position": "end", "content": "MARKER = 1\n", "no_reindent": false}}
```

Fields: `id` (unique string), `op`, `args`, optional `tags` (list of
strings), optional `expect_error: true`.

## Ops and their args

| op | args | notes |
|---|---|---|
| `code` | `target`: qualified path OR `file:A-B` line range | both address modes |
| `callers` | `name`: leaf name or dotted path (leaf is used) | |
| `outline` | `query`: repo-relative file path OR symbol path | |
| `search` | `patterns` (list, ANDed), optional `kind`, `file`, `regex`, `fixed`, `ignore_case`, `limit` | |
| `rename-symbol` | `qualified_path`, `new_name` (bare identifier) | writes |
| `replace-symbol` | `qualified_path`, `content` (full new definition) | writes; a signature edit that renames a parameter propagates to keyword-argument call sites (see below) |
| `delete-symbol` | `qualified_path`, optional `force` | refuses when referenced, unless force |
| `insert-symbol` | `anchor_path`, `position` ∈ {`before`,`after`,`start`,`end`}, `content`, optional `no_reindent` | writes; inserts `content` relative to an anchor symbol |
| `patch` | `file` (repo-relative), `range` "A-B" (1-indexed inclusive), `content` | applied with --force |

### Parameter renames via `replace-symbol`

There is no `rename-parameter` verb. A parameter rename is a CONSEQUENCE of a
signature edit: when a `replace-symbol` on a function-family target
(function/async_function/method/async_method) changes a parameter NAME, the
engine detects it and propagates the rename to every keyword-argument call
site (`f(param=...)` → `f(new=...)`) in the SAME transaction. The def slot and
in-scope body uses come from your replacement `content`; only the cross-call
keyword sites are engine-driven. Positional call arguments are never touched.

Detection is conservative (tier-2 honest). Propagation happens ONLY when:

- the old and new parameter lists have EQUAL arity, AND
- positional pairing yields renames where each new name did NOT exist anywhere
  in the old list AND each old name is gone from the new list (this rules out
  swaps `(a,b)->(b,a)` and reorders).

Multiple simultaneous renames are allowed if each is unambiguous under that
rule. When arity changes, or the pairing is ambiguous, NO propagation happens
and the result carries `signature_changed: true` (with the old/new param
lists) so a caller knows kwarg sites were left untouched. Both implementations
must agree byte-for-byte on the resulting tree.

- To exercise this, author `content` that keeps the function name and arity but
  renames one (or several) parameters, and ensure the corpus has real
  `func(param=...)` keyword call sites — same file and cross-file via import —
  so propagation actually fires.
- Method targets: `content` is a bare top-level `def name(self, ...)` (the only
  way to write a single-symbol body); the engine treats it as compatible with
  the `method` target and discriminates receivers via the same MRO analysis as
  member renames, so decoy same-named methods on unrelated classes stay
  untouched.
- Transaction-backed → undoable (the runner's undo-restores-pristine check
  applies). Kwarg propagation is Python-only; on Go corpora a `replace-symbol`
  simply has no keyword sites to update (Go has no keyword arguments).

### `insert-symbol` (args: `anchor_path`, `position`, `content`, `no_reindent?`)

Inserts `content` relative to the `anchor_path` symbol:

- `before` / `after` — sibling insertion immediately before/after the anchor
  symbol (handles decorated anchors: `after` a decorated def lands past the
  whole decorated block; `before` lands above the first decorator).
- `start` / `end` — inside the anchor (a class/struct body): first/last child
  position.
- `no_reindent: true` sets `--no-reindent` — content is inserted verbatim
  without the engine re-indenting it to the anchor's column. Omit (default
  false) to let the engine reindent. This is a Python-indentation concern;
  Go anchors are brace-delimited so reindent is a no-op there.
- Refuses (`expect_error: true`) on a nonexistent anchor or an invalid
  `position` value.
- NOT transaction-backed (shares the `patch` write path) → `symbol undo` is a
  no-op for it on BOTH implementations; the runner accordingly skips the
  undo-restores-pristine assertion for it (see "Undo semantics" below).

## Tags

- `engine-kind` — historically REQUIRED on every `rename-symbol` /
  `replace-symbol` whose target kind is method/async_method/function/
  async_function/class/constant (Python) or type/var/const (Go), back when
  the Python side ran a receiver-aware v2 engine and the Rust side was
  tier-1 textual. **That divergence no longer exists**: both implementations
  now run the same engine (Rust core ported WP5–WP6; Go renames run the
  `go/types` engine project-wide), so tagged renames are expected to match
  byte-for-byte. Keep tagging the qualifying kinds — the tag is now a
  safety net: IF a residual engine-vs-engine difference ever appears, the
  runner classifies it `known_divergence` instead of `MISMATCH`. A tagged
  op that lands as `known_divergence` today is a regression signal worth
  reporting, not an expected outcome. Renames of non-engine kinds (Python
  `field`, and `insert-symbol`/`patch` of any kind) must NOT carry the tag —
  they are held to hard byte-equality. A `replace-symbol` that renames a
  parameter is still just a `replace-symbol`: tag it `engine-kind` per its
  target kind as usual (the kwarg propagation is held to hard byte-equality).

- `expect_error: true` — for ops both sides must REFUSE (delete a symbol
  with live references without force, rename to a colliding name, code on a
  nonexistent path, patch out-of-bounds range). The runner then asserts
  neither side modified the tree.

## Undo semantics (what the runner asserts per op)

Only **transaction-backed** writes are undoable — `rename-symbol` and
`replace-symbol`. `insert-symbol`, `delete-symbol`, and `patch` share a direct
byte-write path with no transaction (by design, identically on both
implementations), so `symbol undo` is a no-op (`nothing_to_undo`) for them.

The runner matches this: after a successful write it runs `symbol undo` and
asserts the tree returns to pristine for the transaction-backed pair ONLY;
insert/delete/patch skip the undo assertion (their primary-write tree
comparison still applies in full). All five write ops can therefore appear
freely in datasets, in both successful and `expect_error` forms.

## What good coverage looks like

Read the target repo first (its index dump or file listing will be given).
Then aim, for a 100–1,000 op dataset, at roughly 70% reads / 30% writes,
including deliberately nasty cases:

- **Ambiguity**: qualified paths that resolve to multiple rows (same class
  name in two files) → `code` should report candidates on both sides.
- **Suffix resolution**: `code`/`outline` by bare leaf and by partial dotted
  suffix, not just full paths.
- **Deep nesting**: methods of nested classes, closures' parents.
- **Unicode**: files/symbols with non-ASCII content near byte-range edges.
- **Same-leaf collisions**: rename a name that also appears in strings,
  comments, and unrelated symbols. Both engines now agree, so this should
  MATCH; tag `engine-kind` on qualifying kinds as the residual-divergence
  safety net.
- **Field renames with decoys**: rename a `field` (Python) whose leaf name
  also exists as a field on OTHER, unrelated classes (e.g. flask's `key`,
  `methods`, `name`, `data` recur across classes). The field-rename engine
  (index v8) must touch only the target class's field + its references and
  leave same-named decoys on sibling classes untouched — identically on both
  sides. Field renames are NOT `engine-kind`; hold them to hard equality.
- **Constant renames with import/ref sites**: rename a module-level
  `constant`/`const` that is imported or referenced elsewhere; both sides
  must rewrite the same ref set.
- **Inheritance / MRO / super() renames**: rename a method DEFINED on a base
  class that subclasses override or call via `super()`. Pick the base method
  from a real class hierarchy (use the index's class `bases`), so the engine
  must resolve MRO membership and rewrite overrides + `super().method()`
  sites. Tag `engine-kind`.
- **parameter rename via replace-symbol with real kwarg sites**: grep the
  corpus for `func(param=...)` keyword call sites, then author a
  `replace-symbol` whose `content` renames that parameter (same name, same
  arity) so the op exercises cross-call kwarg rewriting, not just the def line.
  Add nasty variants: an arity change (expect `signature_changed`, no
  propagation) and a swap `(a,b)->(b,a)` (ambiguous, no propagation).
- **insert-symbol, all four positions**: `before`/`after`/`start`/`end`,
  including a DECORATED anchor (verify `after` clears the whole decorated
  block).
- **Zero-callers and hot symbols**: callers on names with 0 refs and with
  hundreds.
- **Boundary patches**: first line, last line, single-line file, range
  ending exactly at EOF with no trailing newline.
- **Refusals**: every write op in its expect_error form — colliding rename,
  referenced delete without force, out-of-bounds/missing-file patch, bad
  insert position, nonexistent anchor, unknown rename-parameter param.
- **Go specifics** (Go repos): methods with receivers (receiver-
  discrimination: rename ONE receiver's method, leave same-named methods on
  other receivers untouched), **interface-implicating method renames** (rename
  a method that satisfies an interface — the `go/types` engine should fan the
  rename across every implementer/`affected_interfaces`), const/var blocks,
  type renames, generics, vendored-adjacent paths. Parameter-rename
  propagation does not apply to Go (no keyword arguments), so a Go
  `replace-symbol` is just a body/signature swap.
  `insert-symbol` works on Go anchors (brace-delimited bodies make reindent
  a no-op); include a few successful Go insert/delete/patch ops alongside
  the rename/replace coverage.

Every `qualified_path`/`file` you reference MUST exist in the target repo —
derive them from the provided index dump; do not invent names.
