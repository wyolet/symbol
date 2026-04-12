"""Pipeline registry — pluggable hooks for dep parsing, import resolution, and entry point detection."""

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

# Pipeline categories
DEPS = "deps"
IMPORTS = "imports"
ENTRYPOINTS = "entrypoints"

# Registry: pipeline name → list of (priority, function)
_registry: dict[str, list[tuple[int, Callable]]] = defaultdict(list)


def hook(pipeline: str, priority: int = 100) -> Callable:
    """Register a function as a hook in a pipeline.

    Lower priority runs first. Default is 100.
    Functions receive (project_root: Path, context: dict) and return a list of results.

    Usage:
        @hook("deps")
        def parse_pep621(project_root: Path, context: dict) -> list[str]:
            ...
    """

    def decorator(fn: Callable) -> Callable:
        _registry[pipeline].append((priority, fn))
        return fn

    return decorator


def run_pipeline(pipeline: str, project_root: Path, context: dict | None = None) -> list:
    """Run all hooks in a pipeline, merge results."""
    if context is None:
        context = {}
    hooks = sorted(_registry.get(pipeline, []), key=lambda x: x[0])
    results: list = []
    for _priority, fn in hooks:
        results.extend(fn(project_root, context))
    return results


def get_hooks(pipeline: str) -> list[Callable]:
    """Get all registered hooks for a pipeline (sorted by priority)."""
    return [fn for _, fn in sorted(_registry.get(pipeline, []), key=lambda x: x[0])]


def clear_pipeline(pipeline: str) -> None:
    """Clear all hooks for a pipeline (useful in tests)."""
    _registry[pipeline].clear()
