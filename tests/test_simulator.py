"""Tests for the import simulator — Python-accurate circular import detection."""

from pathlib import Path

from ca_tools.shared.import_graph import build_import_graph
from ca_tools.shared.simulator import simulate_imports


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def test_real_cycle_detected(tmp_path: Path):
    """a imports b.x, b imports a.y — both defined after the import line."""
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x\ny = 1",
            "b.py": "from a import y\nx = 1",
        },
    )
    graph = build_import_graph(tmp_path, propagate_init=False)
    cycles = simulate_imports(graph, tmp_path)
    assert len(cycles) >= 1
    assert cycles[0].failed_name in ("x", "y")


def test_safe_when_name_defined_before(tmp_path: Path):
    """b imports a.y, but y is defined BEFORE the import of b — safe."""
    _make_project(
        tmp_path,
        {
            "a.py": "y = 1\nfrom b import x",
            "b.py": "from a import y\nx = 1",
        },
    )
    graph = build_import_graph(tmp_path, propagate_init=False)
    cycles = simulate_imports(graph, tmp_path)
    assert len(cycles) == 0


def test_submodule_fallback_safe(tmp_path: Path):
    """from . import sub where sub is a file — Python loads it independently."""
    _make_project(
        tmp_path,
        {
            "pkg/__init__.py": "from .a import ClassA",
            "pkg/a.py": "from . import b\nClassA = 'a'",
            "pkg/b.py": "x = 1",
        },
    )
    graph = build_import_graph(tmp_path, propagate_init=False)
    cycles = simulate_imports(graph, tmp_path)
    assert len(cycles) == 0


def test_type_checking_ignored(tmp_path: Path):
    """TYPE_CHECKING imports should not trigger cycles."""
    _make_project(
        tmp_path,
        {
            "a.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from b import x\n",
            "b.py": "from a import y\nx = 1\ny = 1",
        },
    )
    graph = build_import_graph(tmp_path, propagate_init=False)
    cycles = simulate_imports(graph, tmp_path)
    assert len(cycles) == 0


def test_star_import_safe(tmp_path: Path):
    """from a import * gets partial module, doesn't crash."""
    _make_project(
        tmp_path,
        {
            "a.py": "from b import *\ny = 1",
            "b.py": "from a import *\nx = 1",
        },
    )
    graph = build_import_graph(tmp_path, propagate_init=False)
    cycles = simulate_imports(graph, tmp_path)
    # Star imports get whatever is available — not a hard failure
    assert len(cycles) == 0


def test_cycle_explains_failure(tmp_path: Path):
    """Cycle info should include the failed name and reason."""
    _make_project(
        tmp_path,
        {
            "a.py": "from b import B\nclass A: pass",
            "b.py": "from a import A\nclass B: pass",
        },
    )
    graph = build_import_graph(tmp_path, propagate_init=False)
    cycles = simulate_imports(graph, tmp_path)
    assert len(cycles) >= 1
    assert cycles[0].failed_name != ""
    assert cycles[0].reason != ""
    assert "line" in cycles[0].reason or "not defined" in cycles[0].reason
