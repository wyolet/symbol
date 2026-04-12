"""Python language conventions — not framework-specific, just how Python works."""

from pathlib import Path

from ca_tools.shared.pipeline import SKIP_ORPHAN, hook


@hook(SKIP_ORPHAN, priority=10)
def skip_init(_root: Path, _ctx: dict) -> list[str]:
    """__init__.py files are package markers, never orphans."""
    return ["__init__.py"]


@hook(SKIP_ORPHAN, priority=10)
def skip_main(_root: Path, _ctx: dict) -> list[str]:
    """__main__.py files are package entry points, never orphans."""
    return ["__main__.py"]


@hook(SKIP_ORPHAN, priority=20)
def skip_setup_tools(_root: Path, _ctx: dict) -> list[str]:
    """Standard Python project tooling files."""
    return ["setup.py", "manage.py"]
