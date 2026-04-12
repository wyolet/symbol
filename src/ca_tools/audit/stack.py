"""Stack detection — parse dependency files and match against registry."""

import tomllib
from pathlib import Path

from ca_tools.shared.spec import Spec

from .registry import lookup, normalize_package_name


def parse_pyproject_toml(path: Path) -> list[str]:
    """Extract dependency names from pyproject.toml."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []

    deps: list[str] = []

    for dep in data.get("project", {}).get("dependencies", []):
        name = _extract_package_name(dep)
        if name:
            deps.append(name)

    for group_deps in data.get("project", {}).get("optional-dependencies", {}).values():
        for dep in group_deps:
            name = _extract_package_name(dep)
            if name:
                deps.append(name)

    return deps


def parse_requirements_txt(path: Path) -> list[str]:
    """Extract dependency names from requirements.txt."""
    deps: list[str] = []
    try:
        text = path.read_text()
    except OSError:
        return []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = _extract_package_name(line)
        if name:
            deps.append(name)

    return deps


def _extract_package_name(dep_spec: str) -> str | None:
    """Extract bare package name from a dependency specifier like 'requests>=2.0'."""
    name = dep_spec.strip()
    if not name:
        return None
    for i, ch in enumerate(name):
        if ch in ">=<!~[];, @":
            name = name[:i]
            break
    name = name.strip()
    return normalize_package_name(name) if name else None


def detect_deps(project_root: Path) -> list[str]:
    """Find and parse all dependency files in a project."""
    deps: list[str] = []

    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        deps.extend(parse_pyproject_toml(pyproject))

    for req_file in sorted(project_root.glob("requirements*.txt")):
        deps.extend(parse_requirements_txt(req_file))

    seen: set[str] = set()
    unique: list[str] = []
    for d in deps:
        if d not in seen:
            seen.add(d)
            unique.append(d)

    return unique


def detect_stack(project_root: Path, spec: Spec) -> dict[str, list[str]]:
    """Detect the project's tech stack from its dependencies."""
    deps = detect_deps(project_root)
    stack: dict[str, list[str]] = {}

    for dep in deps:
        category = lookup(dep, spec)
        if category is not None:
            stack.setdefault(category, []).append(dep)

    return stack
