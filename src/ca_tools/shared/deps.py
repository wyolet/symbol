"""Stack detection — parse dependency files and match against registry."""

import tomllib
from pathlib import Path

from ca_tools.shared.pipeline import DEPS, hook, run_pipeline
from ca_tools.shared.spec import Spec

from ca_tools.shared.pkg_registry import lookup, normalize_package_name


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


def _get_pyproject(project_root: Path, context: dict) -> dict:
    """Get pyproject.toml data — reads once, caches in context."""
    if "pyproject" not in context:
        pyproject = project_root / "pyproject.toml"
        if not pyproject.exists():
            context["pyproject"] = {}
        else:
            try:
                with open(pyproject, "rb") as f:
                    context["pyproject"] = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError):
                context["pyproject"] = {}
    return context["pyproject"]


# ── Dep parser hooks ─────────────────────────────────────────────────


@hook(DEPS, priority=10)
def parse_pep621(project_root: Path, context: dict) -> list[str]:
    """Parse [project.dependencies] — PEP 621."""
    data = _get_pyproject(project_root, context)
    deps: list[str] = []
    for dep in data.get("project", {}).get("dependencies", []):
        name = _extract_package_name(dep)
        if name:
            deps.append(name)
    return deps


@hook(DEPS, priority=20)
def parse_optional_deps(project_root: Path, context: dict) -> list[str]:
    """Parse [project.optional-dependencies] — PEP 621."""
    data = _get_pyproject(project_root, context)
    deps: list[str] = []
    for group_deps in data.get("project", {}).get("optional-dependencies", {}).values():
        for dep in group_deps:
            name = _extract_package_name(dep)
            if name:
                deps.append(name)
    return deps


@hook(DEPS, priority=30)
def parse_pep735(project_root: Path, context: dict) -> list[str]:
    """Parse [dependency-groups] — PEP 735."""
    data = _get_pyproject(project_root, context)
    deps: list[str] = []
    for group_deps in data.get("dependency-groups", {}).values():
        for dep in group_deps:
            if isinstance(dep, str):
                name = _extract_package_name(dep)
                if name:
                    deps.append(name)
    return deps


@hook(DEPS, priority=40)
def parse_poetry(project_root: Path, context: dict) -> list[str]:
    """Parse [tool.poetry.dependencies] and [tool.poetry.group.*.dependencies]."""
    data = _get_pyproject(project_root, context)
    poetry = data.get("tool", {}).get("poetry", {})
    if not poetry:
        return []

    deps: list[str] = []
    # Main deps
    for pkg in poetry.get("dependencies", {}):
        if pkg.lower() != "python":
            name = normalize_package_name(pkg)
            deps.append(name)
    # Group deps
    for group in poetry.get("group", {}).values():
        for pkg in group.get("dependencies", {}):
            name = normalize_package_name(pkg)
            deps.append(name)
    return deps


@hook(DEPS, priority=50)
def parse_requirements_txt(project_root: Path, _context: dict) -> list[str]:
    """Parse requirements*.txt files."""
    deps: list[str] = []
    for req_file in sorted(project_root.glob("requirements*.txt")):
        try:
            text = req_file.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            name = _extract_package_name(line)
            if name:
                deps.append(name)
    return deps


@hook(DEPS, priority=60)
def parse_setup_cfg(project_root: Path, _context: dict) -> list[str]:
    """Parse install_requires from setup.cfg."""
    setup_cfg = project_root / "setup.cfg"
    if not setup_cfg.exists():
        return []
    try:
        text = setup_cfg.read_text()
    except OSError:
        return []

    deps: list[str] = []
    in_install_requires = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "install_requires =":
            in_install_requires = True
            continue
        if in_install_requires:
            if not stripped or (not line[0].isspace() and "=" in stripped):
                break
            name = _extract_package_name(stripped)
            if name:
                deps.append(name)
    return deps


@hook(DEPS, priority=70)
def parse_pipfile(project_root: Path, _context: dict) -> list[str]:
    """Parse Pipfile [packages] section."""
    pipfile = project_root / "Pipfile"
    if not pipfile.exists():
        return []
    try:
        text = pipfile.read_text()
    except OSError:
        return []

    deps: list[str] = []
    in_packages = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[packages]":
            in_packages = True
            continue
        if stripped.startswith("[") and in_packages:
            break
        if in_packages and "=" in stripped:
            pkg = stripped.split("=")[0].strip().strip('"')
            name = normalize_package_name(pkg)
            if name:
                deps.append(name)
    return deps


# ── Public API ───────────────────────────────────────────────────────


def detect_deps(project_root: Path) -> list[str]:
    """Find and parse all dependency files using the pipeline registry."""
    all_deps = run_pipeline(DEPS, project_root)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for d in all_deps:
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
