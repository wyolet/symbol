# symbol Spec Schema

The spec is a TOML file that defines detection patterns, package classifications,
and checker behavior. It is the single source of truth for what symbol knows
about the Python ecosystem — no behavior is hardcoded in Python.

## Loading and Merging

symbol loads and merges specs from multiple sources in order:

```
1. Built-in spec     (ships with symbol, maintained by the community)
2. Plugin specs      (installed packages that register via entry points)
3. Project spec      (symbol.spec.toml at project root, or inline in pyproject.toml)
```

Later specs override earlier ones on a per-field basis. Lists are merged, scalars
are overridden. The project spec always has the final say.

### Registering a plugin spec

Publish a package and declare an entry point:

```toml
[project.entry-points."symbol.specs"]
my-spec = "my_package:spec_path"
```

Where `spec_path` is a `Path` or string pointing to your `.spec.toml` file.

### Project-local spec

Drop a `symbol.spec.toml` at your project root. It is loaded last and
overrides everything. Use it for internal packages, private libraries, or
project-specific overrides.

---

## Top-level sections

| Section         | Purpose                                          |
|-----------------|--------------------------------------------------|
| `[categories]`  | Display labels for package categories            |
| `[packages]`    | Package registry — identity, type, checker config|
| `[config_files]`| Filenames that indicate project configuration    |
| `[config_dirs]` | Directories that indicate project configuration  |
| `[side_effects]`| Global side effect detection rules               |
| `[entrypoints]` | Entry point detection patterns                   |
| `[frameworks]`  | Framework-specific rule overrides                |

---

## `[categories]`

Maps internal category keys to human-readable display labels.
Every `[packages.X]` entry must reference a category defined here.

```toml
[categories]
web       = "Web framework"
http_client = "HTTP client"
testing   = "Testing"
```

---

## `[packages]`

The package registry. Each entry describes a PyPI package or stdlib module.

### Identity fields

```toml
[packages.requests]
category = "http_client"   # required — must match a key in [categories]
type     = "lib"           # required — lib | tool | app
stdlib   = false           # optional — true for stdlib modules (default: false)
import_name = "requests"   # optional — when import name differs from package name
                           # e.g. Pillow imports as PIL, PyYAML imports as yaml
```

#### `type` values

| Value  | Meaning                                              |
|--------|------------------------------------------------------|
| `lib`  | Pure library — imported by other code                |
| `tool` | CLI/dev tool — installed and run as a command        |
| `app`  | Deployed application — not imported by others        |

`type` affects default checker behavior. Tools get relaxed rules (their setup
code at module level is expected). Apps are rarely in shared specs — they live
in project-local specs.

#### `stdlib = true`

Marks a module as part of the Python standard library. Same schema as PyPI
packages — no special casing. Useful for the unused deps checker (won't look
for it in pyproject.toml) and for display purposes.

```toml
[packages.subprocess]
category = "utility"
type     = "lib"
stdlib   = true
```

### Per-checker config

Each checker can have its own config block under `[packages.X.<checker>]`.
This is where checker-specific behavior is declared — not in the package
identity fields above.

#### `[packages.X.side_effects]`

Controls how the side effects checker treats calls from this package at
module level.

```toml
[packages.requests.side_effects]
module_level = "critical"   # severity if this package is called at module level
                            # debug | info | warning | error | critical
                            # default: warning
```

| Severity   | Meaning                                                    |
|------------|------------------------------------------------------------|
| `debug`    | Expected at module level — suppress by default             |
| `info`     | Informational — shown with --verbose                       |
| `warning`  | Default — worth noting but not a bug                       |
| `error`    | Unexpected — likely a bug                                  |
| `critical` | Never safe at import time — network, I/O, subprocess calls |

Examples:

```toml
[packages.requests.side_effects]
module_level = "critical"   # HTTP call at import time — always a bug

[packages.pytest.side_effects]
module_level = "debug"      # test setup at module level is expected

[packages.celery.side_effects]
module_level = "warning"    # Celery app definition is common but worth noting
```

---

## `[side_effects]`

Global side effect detection rules that apply across all packages.

### `safe_calls`

Leaf function names that are never flagged regardless of which file or package
they come from. Use for constructors and patterns that are universally safe at
module level.

```toml
[side_effects]
safe_calls = ["getLogger", "TypeVar", "NewType", "namedtuple"]
```

### `known_effects`

Leaf function names that are always flagged even if they start with an uppercase
letter (which would normally be treated as a class instantiation and skipped).

```toml
[side_effects]
known_effects = ["load_dotenv", "create_engine", "connect"]
```

### `file_roles`

Maps severity levels to lists of file basenames. Overrides the default `warning`
severity for side effects found in those files.

```toml
[side_effects.file_roles]
debug = ["main.py", "__main__.py", "wsgi.py", "settings.py", "conftest.py"]
error = ["models.py", "utils.py", "schemas.py", "validators.py"]
```

| Severity | Meaning                                                     |
|----------|-------------------------------------------------------------|
| `debug`  | Side effects are expected here — entry points, config files |
| `error`  | Side effects are a bug here — pure logic modules            |

Files not listed get `warning` (the default).

### `package_roles`

Maps severity levels to lists of package import prefixes. When a call at module
level matches a prefix, that severity is used regardless of which file it is in.
Takes precedence over `file_roles`.

```toml
[side_effects.package_roles]
critical = ["requests", "httpx", "subprocess", "socket", "time.sleep"]
debug    = ["logging", "typing"]
```

Prefix matching: `"requests"` matches `requests.get()`, `requests.Session()`, etc.

---

## `[entrypoints]`

Patterns for detecting application entry points.

```toml
[entrypoints]
starters      = ["uvicorn.run", "asyncio.run", "app.run"]
starter_names = ["run", "start", "main", "serve"]
```

---

## `[config_files]` and `[config_dirs]`

Filenames and directory paths that indicate project configuration or deployment
setup. Used by the audit command's project shape summary.

```toml
[config_files]
Dockerfile        = "containerized"
"docker-compose.yml" = "multi-service"
"pyproject.toml"  = "project config"

[config_dirs]
".github/workflows" = "CI/CD"
k8s               = "Kubernetes"
```

---

## `[frameworks]`

Framework-specific overrides. Activated when the framework's package is detected
in the project's dependencies.

```toml
[frameworks.django]
detect = { deps = ["django"] }
skip_orphan_patterns = ["*/migrations/*.py", "*/management/commands/*.py"]
safe_calls = ["setup"]

[frameworks.django.file_roles]
debug = ["apps.py", "admin.py", "signals.py", "urls.py", "manage.py"]
```

### Framework fields

| Field                  | Type           | Purpose                                          |
|------------------------|----------------|--------------------------------------------------|
| `detect.deps`          | list of strings| Activate when any of these packages are declared |
| `detect.config_files`  | list of strings| Activate when any of these files exist at root   |
| `skip_orphan_patterns` | list of globs  | File patterns to exclude from orphan detection   |
| `safe_calls`           | list of strings| Leaf names safe at module level for this framework|
| `file_roles`           | table          | Severity overrides for file basenames            |

Framework `file_roles` are merged on top of the global `[side_effects.file_roles]`.
Framework entries override spec entries for the same filename.

---

## Severity levels

All severity fields use the same enum, aligned with Python's `logging` module:

| Level      | Value  | Meaning                                      |
|------------|--------|----------------------------------------------|
| `debug`    | 0      | Seen but suppressed by default               |
| `info`     | 1      | Informational — shown with --verbose         |
| `warning`  | 2      | Default threshold — worth noting             |
| `error`    | 3      | Real problem — shown prominently             |
| `critical` | 4      | Blocking issue — always shown, exits with 1  |

Default output threshold is `warning`. Pass `--verbose` to see `info` and `debug`.

---

## Contributing to the built-in spec

The built-in spec (`src/ca/symbol/data/spec.toml`) accepts PRs for:

- New packages in `[packages]` — any PyPI package with a clear category
- New `[packages.X.side_effects]` entries — for packages with known module-level danger
- New framework sections — for frameworks with established file conventions

**Acceptance criteria for new packages:**
- Package must be publicly available on PyPI
- Should have meaningful usage in the Python ecosystem
- Popular packages (high download count or GitHub stars) are prioritized

The goal is zero-config usefulness: install symbol, run it, get accurate results
without writing a single line of spec config.
