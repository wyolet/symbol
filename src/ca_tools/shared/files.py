"""Shared file collection with include/exclude glob support."""

from pathlib import Path, PurePath

# Always excluded directories — never parse these, not even for import graph
ALWAYS_SKIP = {
    "__pycache__", "venv", ".venv", "node_modules", "env",
    ".git", ".hg", ".svn",
    "docs", "doc", "docs_src",   # documentation — standalone snippets, not production code
    "scripts", "bin", "tools",   # one-off scripts — not part of the import graph
    "examples", "example",       # demo code
}

# Default exclude patterns — test files add noise to import graph analysis.
# These are always applied; pyproject [tool.ca-tools].exclude adds on top.
DEFAULT_EXCLUDE = [
    "tests/**/*.py",
    "test/**/*.py",
    "test_*.py",
    "*_test.py",
    "conftest.py",
]


def _matches(rel_path: str, pattern: str) -> bool:
    """Match a relative path against a glob pattern, supporting ** for recursive."""
    return PurePath(rel_path).match(pattern)


def collect_py_files(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    skip_defaults: bool = False,
) -> list[Path]:
    """Collect Python files respecting include/exclude glob patterns."""
    all_excludes = list(exclude or [])
    if not skip_defaults:
        all_excludes.extend(DEFAULT_EXCLUDE)

    results: list[Path] = []

    for py_file in sorted(project_root.rglob("*.py")):
        rel = py_file.relative_to(project_root)
        rel_str = str(rel)
        parts = rel.parts

        if any(p.startswith(".") or p in ALWAYS_SKIP for p in parts):
            continue

        if include and not any(_matches(rel_str, pat) for pat in include):
            continue

        if all_excludes and any(_matches(rel_str, pat) for pat in all_excludes):
            continue

        results.append(py_file)

    return results
