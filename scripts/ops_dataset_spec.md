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
| `replace-symbol` | `qualified_path`, `content` (full new definition) | writes |
| `delete-symbol` | `qualified_path`, optional `force` | refuses when referenced, unless force |
| `patch` | `file` (repo-relative), `range` "A-B" (1-indexed inclusive), `content` | applied with --force |

## Tags

- `engine-kind` — REQUIRED on every `rename-symbol` and `replace-symbol`
  whose target kind is method/async_method/function/async_function/class/
  constant (Python) or type/var/const (Go). The Python side routes those
  through its receiver-aware v2 engine while the Rust side is tier-1
  textual; differences are expected and get classified `known_divergence`
  instead of failing the run. Renames of anything else must NOT carry it.

- `expect_error: true` — for ops both sides must REFUSE (delete a symbol
  with live references without force, rename to a colliding name, code on a
  nonexistent path, patch out-of-bounds range). The runner then asserts
  neither side modified the tree.

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
  comments, and unrelated symbols (tier-1 will touch them; engine won't —
  this is exactly the divergence we want measured, tag it `engine-kind`).
- **Zero-callers and hot symbols**: callers on names with 0 refs and with
  hundreds.
- **Boundary patches**: first line, last line, single-line file, range
  ending exactly at EOF with no trailing newline.
- **Refusals**: every write op in its expect_error form.
- **Go specifics** (Go repos): methods with receivers, interface methods,
  const/var blocks, generics, vendored-adjacent paths.

Every `qualified_path`/`file` you reference MUST exist in the target repo —
derive them from the provided index dump; do not invent names.
