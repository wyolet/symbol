"""Path filtering with include/exclude glob support.

File discovery is owned by ``Linguist`` (single project walk, single source
of truth for path → language). This module only filters a caller-supplied
iterable of paths — no walking, no extension matching.
"""

import fnmatch
from collections.abc import Iterable
from pathlib import Path


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


def filter_paths(
    paths: Iterable[Path],
    *,
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Path]:
    """Apply include/exclude glob patterns to a caller-supplied path iterable.

    Dotfile directories (e.g. .git, .venv) are always skipped as a safety net.
    Exclude patterns (from spec.toml [checker] exclude) handle the rest.
    """
    all_excludes = list(exclude or [])
    results: list[Path] = []

    for path in paths:
        try:
            rel = path.relative_to(project_root)
        except ValueError:
            continue
        rel_str = str(rel)
        parts = rel.parts

        if any(p.startswith(".") for p in parts):
            continue

        if include and not any(_matches(rel_str, pat) for pat in include):
            continue

        if all_excludes and any(_matches(rel_str, pat) for pat in all_excludes):
            continue

        results.append(path)

    return results
