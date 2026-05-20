"""Dependency detection — language-agnostic pipeline runner.

The actual file parsers live with their language adapter
(``adapters/python_deps.py``, eventually ``adapters/go_deps.py`` etc).
Each one registers hooks against the ``DEPS`` pipeline at import time;
this module just runs the pipeline and resolves stack categories.
"""

from pathlib import Path

from wyolet.symbol.shared.pipeline import DEPS, run_pipeline
from wyolet.symbol.shared.pkg_registry import lookup
from wyolet.symbol.shared.spec import Spec


def detect_deps(project_root: Path) -> list[str]:
    """Find and parse all dependency files using the pipeline registry."""
    all_deps = run_pipeline(DEPS, project_root)

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
