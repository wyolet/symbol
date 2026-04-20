# Contributing to symbol

## Adding a new package spec

Each package gets its own `src/wyolet/symbol/data/specs/NAME/spec.toml`. Adding a spec requires no Python changes — the spec file is the entire contribution.

### Acceptance threshold

To keep the built-in spec focused on packages people actually encounter in real codebases, new package specs must meet **at least one** of these criteria at the time of the PR:

| Signal | Threshold |
|---|---|
| PyPI monthly downloads | ≥ 50,000 / month |
| GitHub stars | ≥ 500 |
| Ranked in top 1,000 PyPI packages | any position |

**Stdlib modules** are always accepted.  
**Audit-domain packages** (security scanners, linters, static analysis tools) are accepted regardless of count — they're directly in `symbol`'s domain.

#### Once merged, always in

The threshold is an **acceptance gate, not a retention policy**. A package that qualified when merged stays in forever, even if its download count later drops. Removing a spec would break configs in projects already using it, which is worse than keeping a slightly-stale spec.

The only reasons to remove a spec:
- The package is fully abandoned *and* causes systematic false positives that nobody fixes over multiple PRs
- The package was superseded by a successor and the old name now installs something else

### How to check a new package

```bash
uv run scripts/check_pkg_thresholds.py --pkg YOUR_PACKAGE_NAME
```

For CI validation in a PR (exits non-zero if below threshold):

```bash
uv run scripts/check_pkg_thresholds.py --pkg YOUR_PACKAGE_NAME --strict
```

### Spec file format

Create `src/wyolet/symbol/data/specs/NAME/spec.toml`:

```toml
name = "package-name"       # must match the PyPI package name
category = "web"            # must be a key in [categories] in core spec.toml

# Optional fields:
type = "lib"                # lib | tool | app  (default: lib)
stdlib = false              # true for stdlib modules
import_name = "pkg"         # only when import differs from package name (e.g. pillow → PIL)
runtime_only = false        # true for packages that are deps but not imported (e.g. gunicorn)

[detect]
deps = ["package-name"]             # pip name(s) that indicate this package is active
config_files = ["package.cfg"]      # config file basenames that signal this package

[orphan]
patterns = ["*/migrations/*.py"]    # glob patterns for files that are conventionally not imported

[side_effects]
module_level = "warning"            # severity if imported at module level: debug|info|warning|error|critical
safe_calls = ["setup"]              # call names that are expected at module level

[side_effects.file_roles]
debug = ["apps.py", "signals.py"]   # files where module-level side effects are expected (lower severity)
```

Then add the package name to `[specs] include` in `src/wyolet/symbol/data/spec.toml`.

### Running tests

```bash
uv run pytest
```

All tests must pass. The spec loader validates that every package in `[specs] include` has a valid spec file and that all categories and severities are known values.

## Project-local spec extensions

Projects can ship their own package specs without contributing them upstream. Useful for internal packages, monorepo-specific tools, or packages that don't meet the public threshold.

In the project's `pyproject.toml`:

```toml
[tool.symbol]
extra_specs = [
    ".symbol/specs/my-internal-sdk.toml",
    ".symbol/specs/company-auth.toml",
]
```

Each file follows the same format as built-in specs. Extra specs are loaded after built-ins and override them if the package name matches — so you can also use this to patch a built-in spec without touching the `symbol` source.

## Other contributions

- **Checker improvements** — see `src/wyolet/symbol/checkers/`
- **CLI commands** — see `src/wyolet/symbol/commands/`
- **Bug reports** — open an issue at https://github.com/anthropics/ca-tools/issues
