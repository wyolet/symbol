# Differential-test ops datasets

JSONL ops datasets for `scripts/diff_runner.py`, which executes each op
against BOTH implementations of symbol (Python oracle CLI + Rust
`symbol-rs`) and diffs the results. Authored per the rules in
`scripts/ops_dataset_spec.md`. **Every target is derived from a real index
dump of the corpus — nothing is invented.** JSONL has no comment syntax, so
provenance and composition live here.

## Corpus provenance

Corpora are ephemeral clones under a scratchpad dir (not committed here).
The runner copies each corpus per write op, so nothing below is mutated.

| corpus | source | commit (`git rev-parse HEAD`) | language | indexed |
|---|---|---|---|---|
| `flask` | github.com/pallets/flask | `36e4a824f340fdee7ed50937ba8e7f6bc7d17f81` | Python | 80 files → 1831 symbols |
| `gin` | github.com/gin-gonic/gin | `34dac209ffb6ef85cc78c5d217bbb7ad001d68fd` | Go | 98 files → 1659 symbols |
| `self` | this repo (`github.com/wyolet/symbol`) | `5f82bcce19b541d8a5e4f7ba7ef99697c474e838` | Python (+ Go fixtures) | 159 files → 2283 symbols |

`self` is a clean tree materialized with `git archive HEAD | tar -x` (no
`.git`, no `.symbol`, no working-tree noise) so write ops never touch the
reviewer's checkout. The smoke files (`engine-ops.jsonl`, `smoke-ops.jsonl`,
scratchpad) run against the local `fixture-proj`
(`e38a36b7aa730f89f8a4f6ff297467efed48e198`, no remote).

## Composition

Target: ~70% reads / 30% writes, deliberately-nasty coverage.

| dataset | ops | reads (code/callers/outline/search) | writes | expect_error |
|---|---|---|---|---|
| `flask.jsonl` | 426 | 316 (158 / 44 / 55 / 59) | 110 | 11 |
| `gin.jsonl` | 383 | 290 (137 / 44 / 55 / 54) | 93 | 7 |
| `self.jsonl` | 385 | 319 (160 / 44 / 55 / 60) | 66 | 6 |

Write op mix (successful + expect_error forms):

| dataset | rename-symbol | replace-symbol | rename-parameter | insert-symbol | patch | delete-symbol |
|---|---|---|---|---|---|---|
| `flask.jsonl` | 72 | 3 | 12 | 10 | 7 | 6 |
| `gin.jsonl` | 79 | 4 | 0 | 3 | 3 | 4 |
| `self.jsonl` | 46 | 1 | 11 | 3 | 2 | 3 |

### Coverage highlights

- **flask** — ambiguous qpaths (`flask.cli.locate_app` ×3,
  `flask.sansio.app.App.template_filter` ×3, …); suffix/bare-leaf addressing;
  field renames with same-name decoys on sibling classes (`key`, `methods`,
  `name`, `data`, `cli`, `url_prefix`, `session_interface`); constant renames
  (`flask.config.T`, `flask.ctx.F`, …); method renames across a real
  inheritance hierarchy (`flask.views.View.dispatch_request`/`as_view` —
  `MethodView` overrides + `super()` sites); rename-parameter on functions
  with real `param=` kwarg sites; insert-symbol in all four positions incl.
  a decorated anchor (`flask.cli.routes_command`) and a `no_reindent` case;
  boundary patches (first/second line, EOF); force-deletes of unreferenced
  constants; and every write op in its `expect_error` form.
- **gin** — Go type/var/const/method/function renames; interface-implicating
  method renames (`headerBinding.Bind`, `JSON.Render`, `XML.WriteContentType`
  — the `go/types` engine fans across implementers); receiver-discrimination
  (`responseWriter.Write` vs `failWriter.Write` — rename one, leave the
  other); zero-caller consts + hot names (`Context`, `Header`, `String`);
  successful Go insert-symbol (before/after), boundary patches, force-deletes
  of zero-ref consts; `expect_error` for every write op. rename-parameter is
  skipped entirely (no Go kwargs).
- **self** — dogfood: internal `_`-prefixed function/method renames, class,
  constant, and field renames, rename-parameter on single-line-signature
  functions, insert-symbol (after / class-end), first-line patches, a
  zero-ref force-delete, `expect_error` refusals.

## Undo semantics

Only `rename-symbol`, `replace-symbol`, and `rename-parameter` are
transaction-backed and therefore undoable; `insert-symbol`, `delete-symbol`,
and `patch` are direct byte-writes for which `symbol undo` is a no-op — by
design, identically on both implementations. The runner mirrors this: the
undo-restores-pristine assertion applies to the transaction-backed trio
only, so all six write ops appear freely in these datasets. See
`../ops_dataset_spec.md` § "Undo semantics".

## Sweep results (2026-07-08)

See the "Differential verification" section of `../../rust/symbol/README.md`
for the full verdict and timing tables, and `reports/` for raw JSON.
Summary: index diff 100% match on all corpora; all writes byte-identical
with clean undo round-trips; every MISMATCH reduces to (1) Rust returning
empty `signature` for Go symbols on reads, (2) one flask search
limit-truncation ordering case, (3) a diff_runner artifact — `py_write`
passes replace-symbol content positionally but the Python CLI requires
`--content`, so the two successful flask replace ops false-MISMATCH
(manually verified identical when invoked correctly).

## Regeneration

Datasets are produced by `scripts/datasets/gen.py` (with `enumerate.py` +
`sigs.py`, all kept alongside these datasets), which read per-corpus index
dumps and emit JSONL. Corpora + intermediate `enum-*.json`/`sigs-*.json`
dumps are ephemeral scratchpad artifacts:

```
# 1. index each corpus with the Rust binary
rust/target/release/symbol-rs --root <corpus> index
# 2. dump index facts (qpaths, kinds, files, bases, ref/caller counts, signatures)
uv run python enumerate.py <corpus> enum-<name>.json
uv run python sigs.py     <corpus> sigs-<name>.json
# 3. author the dataset from the real dumps
python gen.py <name> enum-<name>.json sigs-<name>.json scripts/datasets/<name>.jsonl
```

Reports land in `scripts/datasets/reports/<name>-report.json`; run via
`uv run scripts/diff_runner.py <corpus> scripts/datasets/<name>.jsonl --report <out>`.
