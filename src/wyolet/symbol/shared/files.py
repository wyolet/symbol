"""Shared file collection with include/exclude glob support."""

import fnmatch
from pathlib import Path, PurePath


def _matches(rel_path: str, pattern: str) -> bool:
    """Match a relative path against a glob pattern, supporting ** for recursive.

    Handles patterns like **/venv/** by also trying without the leading **/ prefix,
    so that paths at the root level are matched correctly.
    """
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    # For **/X/... patterns, also match paths that start at the root (no leading dir)
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(rel_path, pattern[3:])
    return False


def collect_py_files(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Path]:
    """Collect Python files respecting include/exclude glob patterns.

    Dotfile directories (e.g. .git, .venv) are always skipped as a safety net.
    Exclude patterns (from spec.toml [checker] exclude) handle the rest.
    """
    all_excludes = list(exclude or [])

    results: list[Path] = []

    for py_file in sorted(project_root.rglob("*.py")):
        rel = py_file.relative_to(project_root)
        rel_str = str(rel)
        parts = rel.parts

        if any(p.startswith(".") for p in parts):
            continue

        if include and not any(_matches(rel_str, pat) for pat in include):
            continue

        if all_excludes and any(_matches(rel_str, pat) for pat in all_excludes):
            continue

        results.append(py_file)

    return results
