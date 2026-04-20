"""Checker registry — register checkers with their view functions."""

from collections.abc import Callable
from dataclasses import dataclass, field

from wyolet.symbol.shared.checker import CheckerInfo
from wyolet.symbol.shared.findings import Severity


@dataclass
class CheckerEntry:
    """A registered checker with its detection and view functions."""

    info: CheckerInfo
    detect: Callable  # file: (ctx, path, tree) -> list[T], project: (ctx) -> list[T]
    to_findings: Callable | None = None  # (items, ctx) -> list[Finding]
    rich_view: Callable | None = None  # (items, ctx, console) -> None
    json_view: Callable | None = None  # (items, ctx) -> Any


_registry: dict[str, CheckerEntry] = {}


def register(
    name: str,
    *,
    description: str,
    kind: str,
    default_severity: Severity = Severity.WARNING,
    contributes_to_report: bool = True,
    priority: int = 100,
) -> Callable:
    """Decorator — registers a checker function."""

    def decorator(fn: Callable) -> Callable:
        if name in _registry:
            raise ValueError(f"Checker {name!r} already registered")
        info = CheckerInfo(
            name=name,
            description=description,
            kind=kind,
            default_severity=default_severity,
            contributes_to_report=contributes_to_report,
            priority=priority,
        )
        _registry[name] = CheckerEntry(info=info, detect=fn)
        return fn

    return decorator


def views(
    name: str,
    *,
    rich: Callable | None = None,
    json: Callable | None = None,
    findings: Callable | None = None,
) -> None:
    """Register view functions for a checker."""
    entry = _registry.get(name)
    if entry is None:
        raise ValueError(f"Checker {name!r} not registered — call @register first")
    if rich is not None:
        entry.rich_view = rich
    if json is not None:
        entry.json_view = json
    if findings is not None:
        entry.to_findings = findings


def get_all() -> list[CheckerEntry]:
    """Return all registered checkers sorted by priority."""
    return sorted(_registry.values(), key=lambda e: e.info.priority)


def get(name: str) -> CheckerEntry | None:
    return _registry.get(name)


def load_custom_checkers(paths: list[str], project_root: "Path") -> list[str]:
    """Import custom checker modules from paths relative to project_root.

    Each module is expected to call @register at import time. Returns names of
    successfully loaded modules; logs warnings for failures.
    """
    import importlib.util
    from pathlib import Path

    loaded: list[str] = []
    for rel_path in paths:
        full = (project_root / rel_path).resolve()
        if not full.exists():
            import warnings
            warnings.warn(f"Custom checker not found: {full}", stacklevel=2)
            continue
        spec = importlib.util.spec_from_file_location(f"_ca_custom_{full.stem}", full)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            loaded.append(rel_path)
        except Exception as exc:
            import warnings
            warnings.warn(f"Failed to load custom checker {rel_path}: {exc}", stacklevel=2)
    return loaded


def clear() -> None:
    """For tests."""
    _registry.clear()
