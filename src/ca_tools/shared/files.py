"""Shared file collection with include/exclude glob support."""

from pathlib import Path, PurePath


def _matches(rel_path: str, pattern: str) -> bool:
    """Match a relative path against a glob pattern, supporting ** for recursive."""
    return PurePath(rel_path).match(pattern)


def collect_py_files(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    skip_patterns: tuple[str, ...] | None = None,
    skip_dirs: frozenset[str] | None = None,
) -> list[Path]:
    """Collect Python files respecting include/exclude glob patterns.

    skip_patterns — extra glob patterns to exclude (e.g. from spec.toml [files] skip_patterns).
                    When None, no extra patterns are applied.
    skip_dirs     — directory names to skip entirely (no AST, no analysis).
                    Comes from spec.toml [files] skip_dirs, merged with project config.
                    Linguist LOC counting is unaffected (it scans files independently).
    """
    all_excludes = list(exclude or [])
    if skip_patterns:
        all_excludes.extend(skip_patterns)

    effective_skip = skip_dirs or frozenset()

    results: list[Path] = []

    for py_file in sorted(project_root.rglob("*.py")):
        rel = py_file.relative_to(project_root)
        rel_str = str(rel)
        parts = rel.parts

        if any(p.startswith(".") or p in effective_skip for p in parts):
            continue

        if include and not any(_matches(rel_str, pat) for pat in include):
            continue

        if all_excludes and any(_matches(rel_str, pat) for pat in all_excludes):
            continue

        results.append(py_file)

    return results
