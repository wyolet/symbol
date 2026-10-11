# Differential-test ops datasets

JSONL ops datasets for `scripts/diff_runner.py`, which executes each op
against BOTH implementations of symbol (Python oracle CLI + Rust
`symbol` binary) and diffs the results. Authored per the rules in
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
| `flask.jsonl` | 425 | 316 (158 / 44 / 55 / 59) | 109 | 9 |
| `gin.jsonl` | 383 | 290 (137 / 44 / 55 / 54) | 93 | 7 |
| `self.jsonl` | 384 | 319 (160 / 44 / 55 / 60) | 65 | 5 |

Write op mix (successful + expect_error forms). Parameter renames are
`replace-symbol` signature edits (`rp*` ids), not a separate op:

| dataset | rename-symbol | replace-symbol | insert-symbol | patch | delete-symbol |
|---|---|---|---|---|---|
| `flask.jsonl` | 72 | 14 | 10 | 7 | 6 |
| `gin.jsonl` | 79 | 4 | 3 | 3 | 4 |
| `self.jsonl` | 46 | 11 | 3 | 2 | 3 |

### Coverage highlights

- **flask** — ambiguous qpaths (`flask.cli.locate_app` ×3,
  `flask.sansio.app.App.template_filter` ×3, …); suffix/bare-leaf addressing;
  field renames with same-name decoys on sibling classes (`key`, `methods`,
  `name`, `data`, `cli`, `url_prefix`, `session_interface`); constant renames
  (`flask.config.T`, `flask.ctx.F`, …); method renames across a real
  inheritance hierarchy (`flask.views.View.dispatch_request`/`as_view` —
  `MethodView` overrides + `super()` sites); signature parameter edits on
  functions with real `param=` kwarg sites; insert-symbol in all four positions incl.
  a decorated anchor (`flask.cli.routes_command`) and a `no_reindent` case;
  boundary patches (first/second line, EOF); force-deletes of unreferenced
  constants; and every write op in its `expect_error` form.
- **gin** — Go type/var/const/method/function renames; interface-implicating
  method renames (`headerBinding.Bind`, `JSON.Render`, `XML.WriteContentType`
  — the `go/types` engine fans across implementers); receiver-discrimination
  (`responseWriter.Write` vs `failWriter.Write` — rename one, leave the
  other); zero-caller consts + hot names (`Context`, `Header`, `String`);
  successful Go insert-symbol (before/after), boundary patches, force-deletes
  of zero-ref consts; `expect_error` for every write op. No parameter edits
  (no Go kwargs).
- **self** — dogfood: internal `_`-prefixed function/method renames, class,
  constant, and field renames, parameter edits on single-line-signature
  functions, insert-symbol (after / class-end), first-line patches, a
  zero-ref force-delete, `expect_error` refusals.

## Undo semantics

Only `rename-symbol` and `replace-symbol` are
transaction-backed and therefore undoable; `insert-symbol`, `delete-symbol`,
and `patch` are direct byte-writes for which `symbol undo` is a no-op — by
design, identically on both implementations. The runner mirrors this: the
undo-restores-pristine assertion applies to the transaction-backed pair
only, so all five write ops appear freely in these datasets. See
`../ops_dataset_spec.md` § "Undo semantics".

## Reading results

Verdict tables live in the "Differential verification" section of
`../../rust/symbol/README.md`; raw JSON in `reports/`. A non-`expect_error`
write that both sides refuse is reported as `both_refused`, not `match` —
it proves the refusals agree but exercises no write path.

## Regeneration

Datasets are produced by `scripts/datasets/gen.py` (with `enumerate.py` +
`sigs.py`, all kept alongside these datasets), which read per-corpus index
dumps and emit JSONL. Corpora + intermediate `enum-*.json`/`sigs-*.json`
dumps are ephemeral scratchpad artifacts:

```
# 1. index each corpus with the Rust binary
rust/target/release/symbol --root <corpus> index
# 2. dump index facts (qpaths, kinds, files, bases, ref/caller counts, signatures)
uv run python enumerate.py <corpus> enum-<name>.json
uv run python sigs.py     <corpus> sigs-<name>.json
# 3. author the dataset from the real dumps
python gen.py <name> enum-<name>.json sigs-<name>.json scripts/datasets/<name>.jsonl
```

Reports land in `scripts/datasets/reports/<name>-report.json`; run via
`uv run scripts/diff_runner.py <corpus> scripts/datasets/<name>.jsonl --report <out>`.

## MCP mode

```
uv run scripts/diff_runner.py <corpus> scripts/datasets/<name>.jsonl --mcp [--report <out>]
```

Starts both MCP servers (`uv run symbol mcp` and `rust/target/release/symbol
mcp`), each on its own copy of the corpus with the copy as working directory,
and compares full tool results: the text payload byte-for-byte after each
copy's root is replaced with `<root>` and transaction ids are masked.

- `initialize` (minus `serverInfo.version`) and `tools/list` (names, order,
  descriptions, input/output schemas) must be identical.
- Reads share one server pair: `code` → `SymbolBody` (plain, and again with
  `include_refs` + `offset`/`limit`), `callers` → `SymbolCallers`, `outline` →
  `SymbolOutline`, `search` → `SearchSymbol`.
- Each write gets fresh copies and a fresh pair: a `dry_run` call, then the
  real call (`patch` runs without `force`, so the first call must come back
  `needs_read_confirmation` and the retry must pass), then the tree-state
  verdict as in CLI mode, `SymbolOutline` of the touched files, `Undo` for
  rename/replace (both trees must return to pristine), and `Refresh`.
  Searches after a write compare hits as a set: both servers re-index the
  touched files in set-iteration order, which Python randomizes per process.
- Every successful `patch` op is replayed as `MultiPatch` (range-addressed,
  with the same read-then-retry contract, then an `old`-addressed edit).

Verdicts use the same vocabulary; every compared call counts once.
`known_divergence` marks a Go `RenameSymbol` where only the Rust text lists
affected interface contracts: the Python Go adapter clears that list on the
first call and answers a repeated identical rename (the real call after its
`dry_run`) from a cached analysis without it.
