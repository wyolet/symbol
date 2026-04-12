"""pytest framework hooks — conftest.py and test file conventions."""

from pathlib import Path

from ca_tools.shared.pipeline import SKIP_ORPHAN, hook


@hook(SKIP_ORPHAN, priority=30)
def skip_conftest(_root: Path, _ctx: dict) -> list[str]:
    """conftest.py files are auto-discovered by pytest."""
    return ["conftest.py"]


@hook(SKIP_ORPHAN, priority=30)
def skip_test_files(_root: Path, _ctx: dict) -> list[str]:
    """test_*.py and *_test.py are discovered by pytest, not imported."""
    return ["test_*.py", "*_test.py"]
