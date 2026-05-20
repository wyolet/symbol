# Package Spec Schema

A **package spec** teaches `symbol` what's idiomatic for a single library or
framework — which files aren't really orphans, which module-level calls are
expected, which decorators mark entry points. Each spec is one TOML file:

```
src/wyolet/symbol/data/specs/<package>/spec.toml
```

237 specs ship today. New ones are welcome — see issue [#3](https://github.com/wyolet/symbol/issues/3).

The contract is described formally in
[`schemas/symbol.spec.schema.json`](../schemas/symbol.spec.schema.json) (JSON
Schema draft 2020-12) and enforced by `tests/test_spec_schema.py`, which
validates every bundled spec on every PR. The prose below is a friendlier tour
of the same shape.

> **Looking for project-level config?** That's a different file
> (`symbol.toml` / `[tool.symbol]`) with its own schema at
> [`schemas/symbol.config.schema.json`](../schemas/symbol.config.schema.json).
> This doc covers the per-package specs that ship inside `symbol` itself.

---

## Worked example

A typical spec for a web framework with router-style wiring:

```toml
name     = "aiogram"
category = "messaging"
type     = "lib"
detect   = { deps = ["aiogram"] }

[checkers.orphan]
# Files users write but only the framework calls — not dead code.
patterns = ["handlers.py", "callbacks.py", "middlewares.py", "bot.py"]

[checkers.side_effects.calls]
# Bare function names. `skip` silences a call entirely; other severities
# escalate it.
skip = ["include_router", "include_routers", "Dispatcher", "Bot", "Router"]

[checkers.side_effects.patterns]
# File-basename or path globs grouped by severity. Module-level side effects
# inside these files get demoted to `debug` (hidden by default).
debug = ["handlers.py", "callbacks.py", "middlewares.py", "bot.py"]
```

Compare with [`data/specs/fastapi/spec.toml`](../src/wyolet/symbol/data/specs/fastapi/spec.toml)
for the canonical larger example.

---

## Top-level fields

| Field          | Required | Type                  | Default | Purpose                                                                |
|----------------|----------|-----------------------|---------|------------------------------------------------------------------------|
| `name`         | ✅       | string                | —       | PyPI distribution name (or stdlib module name).                        |
| `category`     | ✅       | string                | —       | Category key — must exist in `[categories]` of `data/spec.toml`.       |
| `type`         |          | `lib` \| `tool` \| `app` | `lib`   | Role: library, CLI/dev tool, or application.                           |
| `stdlib`       |          | bool                  | `false` | Stdlib modules are always loaded, even when not in project deps.       |
| `import_name`  |          | string                | —       | Use when the import name differs (e.g. Pillow → `PIL`, PyYAML → `yaml`). |
| `runtime_only` |          | bool                  | `false` | Used at runtime but never imported in source (e.g. `gunicorn`).        |
| `detect`       |          | table                 | `{}`    | Heuristics for marking this package active in a project.               |
| `checker`      |          | table                 | `{}`    | AST-checker file filters applied when active.                          |
| `scanner`      |          | table                 | `{}`    | LOC-scanner file filters applied when active.                          |
| `checkers`     |          | table                 | `{}`    | Per-checker configuration (see below).                                 |

Unknown top-level keys are rejected by the schema — typos fail fast.

### `[detect]`

How `symbol` decides this spec is active for the current project.

```toml
[detect]
deps         = ["aiogram"]              # PyPI names — any match activates
config_files = ["alembic.ini"]          # filenames at project root
```

### `[checker]` and `[scanner]`

Glob excludes only — the AST checker and LOC scanner each accept an `exclude`
list:

```toml
[checker]
exclude = ["docs_src/**"]

[scanner]
exclude = ["vendor/**"]
```

---

## `[checkers.orphan]`

Files that should never be flagged as orphans, even with no callers. Used for
framework conventions where the user writes the file and the framework calls
it.

```toml
[checkers.orphan]
patterns = ["handlers.py", "callbacks.py", "admin.py"]
```

Entries are matched against the file's path within the project. Plain
basenames (`admin.py`) and globs (`**/migrations/*.py`) both work.

---

## `[checkers.side_effects]`

Where most spec authoring happens. Three sub-fields:

### `severity`

Default severity for module-level side effects attributed to this package.
Affects calls that aren't otherwise classified by `calls` or `patterns`.

```toml
[checkers.side_effects]
severity = "critical"     # e.g. for requests — module-level HTTP is always bad
```

Allowed values: `skip`, `debug`, `info`, `warning` (default), `error`, `critical`.

### `calls` — bare function names, grouped by severity

```toml
[checkers.side_effects.calls]
skip  = ["include_router", "add_middleware", "FastAPI", "APIRouter"]
error = ["load_dotenv", "create_engine"]
```

- Keys are severities. `skip` silences entirely; the others escalate.
- Values are **bare identifiers** — `include_router`, never `dp.include_router(...)`
  or `app.include_router()`.
- A call matches if its leaf name appears in any of these lists.

### `patterns` — file globs, grouped by severity

```toml
[checkers.side_effects.patterns]
debug = ["handlers.py", "lifespan.py", "tests/**"]
error = ["models.py", "schemas.py"]
```

- Keys are severities. `debug` demotes (hidden by default); `error`/`critical`
  promote.
- Values are file basenames or path globs.
- A side effect's severity is overridden if its source file matches.

---

## Severity enum

Used throughout — every place that takes a severity accepts exactly these
strings:

| Value      | Default behavior                                      |
|------------|-------------------------------------------------------|
| `skip`     | Suppressed entirely — not recorded.                   |
| `debug`    | Recorded but hidden; surface with `-v`.               |
| `info`     | Informational; surface with `-v`.                     |
| `warning`  | Default output threshold.                             |
| `error`    | Real problem — exit non-zero in CI.                   |
| `critical` | Blocking — always shown.                              |

---

## Legacy aliases

Two older shapes are still accepted, but new specs should not use them. Both
get migrated into the canonical form at load time.

```toml
# Legacy — equivalent to [checkers.orphan]
[orphan]
patterns = ["..."]

# Legacy — equivalent to [checkers.side_effects]
[side_effects]
module_level = "warning"           # → severity
safe_calls   = ["FastAPI"]         # → calls.skip
known_effects = ["load_dotenv"]    # → calls.error
[side_effects.file_roles]          # → patterns
debug = ["main.py"]
```

The schema validates these aliases too, so they won't silently bit-rot — but
prefer the `[checkers.*]` form when writing new specs.

---

## Validating your spec

Before opening a PR, validate locally:

```bash
make validate          # or: uv run --extra dev pytest tests/test_spec_schema.py -v
```

Other handy targets: `make test`, `make lint`, `make audit` (dogfood). Run
`make` with no args for the full list.

The test walks every `data/specs/*/spec.toml`, validates it against
`schemas/symbol.spec.schema.json`, and also runs it through the live loader.
A failure prints the exact path of the offending key, e.g.:

```
aiogram: checkers.orphan: Additional properties are not allowed ('filenames' was unexpected)
```

CI runs this on every PR, so a spec that doesn't validate cannot land.

### Manual one-off check

If you want to validate a single file without running pytest:

```bash
uv run --with jsonschema python -c "
import json, tomllib, sys
from pathlib import Path
from jsonschema import Draft202012Validator
schema = json.loads(Path('schemas/symbol.spec.schema.json').read_text())
raw = tomllib.loads(Path(sys.argv[1]).read_text())
for err in Draft202012Validator(schema).iter_errors(raw):
    path = '.'.join(str(p) for p in err.absolute_path) or '<root>'
    print(f'{path}: {err.message}')
" path/to/spec.toml
```

### Editor integration

Wire the schema into [Taplo](https://taplo.tamasfe.dev/) for live validation
in your editor:

```toml
# .taplo.toml at repo root
[[rule]]
include = ["src/wyolet/symbol/data/specs/*/spec.toml"]
schema  = { path = "schemas/symbol.spec.schema.json" }
```

---

## Contributing a new spec

1. Pick a package from issue [#3](https://github.com/wyolet/symbol/issues/3) or
   propose your own.
2. Read a spec for a similar package — `data/specs/fastapi/spec.toml` for web
   frameworks, `data/specs/celery/spec.toml` for task queues,
   `data/specs/pytest/spec.toml` for test tools.
3. Create `src/wyolet/symbol/data/specs/<package>/spec.toml`.
4. Add the package name to the `specs.include` list in
   `src/wyolet/symbol/data/spec.toml`.
5. Run `uv run pytest tests/test_spec_schema.py` — must be green.
6. Run `symbol audit` on a real project that uses the package; confirm false
   positives drop.
7. Open a PR. One package per PR keeps review easy.
