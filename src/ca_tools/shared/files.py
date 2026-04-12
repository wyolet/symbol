"""Shared file collection with include/exclude glob support."""

from pathlib import Path, PurePath

# Always excluded directories
ALWAYS_SKIP = {"__pycache__", "venv", ".venv", "node_modules", "env", ".git", ".hg", ".svn"}


def _matches(rel_path: str, pattern: str) -> bool:
    """Match a relative path against a glob pattern, supporting ** for recursive."""
    return PurePath(rel_path).match(pattern)


def collect_py_files(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Path]:
    """Collect Python files respecting include/exclude glob patterns."""
    results: list[Path] = []

    for py_file in sorted(project_root.rglob("*.py")):
        rel = py_file.relative_to(project_root)
        rel_str = str(rel)
        parts = rel.parts

        if any(p.startswith(".") or p in ALWAYS_SKIP for p in parts):
            continue

        if include and not any(_matches(rel_str, pat) for pat in include):
            continue

        if exclude and any(_matches(rel_str, pat) for pat in exclude):
            continue

        results.append(py_file)

    return results
