"""Tests for import map analysis."""

from pathlib import Path

from ca_tools.map.analyzer import analyze_map


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def test_detects_cycle(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x",
            "b.py": "from a import y\nx = 1",
        },
    )
    result = analyze_map(tmp_path)
    assert len(result.cycles) >= 1
    # Both a.py and b.py should appear in a cycle
    cycle_files = set()
    for c in result.cycles:
        cycle_files.update(c.path)
    assert "a.py" in cycle_files
    assert "b.py" in cycle_files


def test_no_cycles(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x",
            "b.py": "x = 1",
        },
    )
    result = analyze_map(tmp_path)
    assert len(result.cycles) == 0


def test_hotspots(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "core.py": "x = 1",
            "a.py": "from core import x",
            "b.py": "from core import x",
            "c.py": "from core import x",
        },
    )
    result = analyze_map(tmp_path, min_fan_in=3)
    assert len(result.hotspots) == 1
    assert result.hotspots[0].module == "core.py"
    assert result.hotspots[0].fan_in == 3


def test_fragile(tmp_path: Path):
    # Create a module that imports many others
    _make_project(
        tmp_path,
        {
            "m1.py": "x = 1",
            "m2.py": "x = 2",
            "m3.py": "x = 3",
            "main.py": "from m1 import x\nfrom m2 import x\nfrom m3 import x",
        },
    )
    result = analyze_map(tmp_path, min_fan_out=3)
    assert len(result.fragile) == 1
    assert result.fragile[0].module == "main.py"
    assert result.fragile[0].fan_out == 3


def test_leaves(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "core.py": "x = 1",
            "util.py": "y = 2",
            "main.py": "from core import x\nfrom util import y",
            "other.py": "from core import x",
        },
    )
    result = analyze_map(tmp_path)
    leaf_modules = [leaf.module for leaf in result.leaves]
    # util.py is only imported by main.py
    assert "util.py" in leaf_modules
    # core.py is imported by 2 files, not a leaf
    assert "core.py" not in leaf_modules


def test_deep_chains(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x",
            "b.py": "from c import x\nx = 1",
            "c.py": "from d import x\nx = 1",
            "d.py": "x = 1",
        },
    )
    # Chain is 4 deep (a → b → c → d), set threshold to 4
    result = analyze_map(tmp_path, min_chain_depth=4)
    assert len(result.deep_chains) >= 1
    assert len(result.deep_chains[0]) == 4


def test_deep_chains_filtered(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x",
            "b.py": "from c import x\nx = 1",
            "c.py": "x = 1",
        },
    )
    # Chain is 3 deep, threshold is 5 — should find nothing
    result = analyze_map(tmp_path, min_chain_depth=5)
    assert len(result.deep_chains) == 0


def test_exclude_pattern(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "src/app.py": "from src.lib import x",
            "src/lib.py": "x = 1",
            "vendor/pkg.py": "y = 1",
        },
    )
    result = analyze_map(tmp_path, exclude=["vendor/*"])
    all_modules = set()
    for leaf in result.leaves:
        all_modules.add(leaf.module)
    # vendor files should not appear
    assert not any("vendor" in m for m in all_modules)


def test_empty_project(tmp_path: Path):
    result = analyze_map(tmp_path)
    assert result.total_files == 0
    assert result.total_edges == 0
    assert result.cycles == []
    assert result.hotspots == []


def test_init_reexport_not_a_cycle(tmp_path: Path):
    """__init__.py importing from submodules is a re-export, not a cycle."""
    _make_project(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg.models import Model\nfrom pkg.utils import helper",
            "pkg/models.py": "from pkg import utils\nModel = 'model'",
            "pkg/utils.py": "helper = 'helper'",
        },
    )
    result = analyze_map(tmp_path)
    assert len(result.cycles) == 0


def test_real_cross_package_cycle_detected(tmp_path: Path):
    """Cycles between different packages are real and should be reported."""
    _make_project(
        tmp_path,
        {
            "pkg_a/__init__.py": "from pkg_b import x",
            "pkg_b/__init__.py": "from pkg_a import y\nx = 1",
        },
    )
    result = analyze_map(tmp_path)
    assert len(result.cycles) >= 1


def test_init_self_reference_suppressed(tmp_path: Path):
    """__init__.py referencing itself is noise, not a cycle."""
    _make_project(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg import sub\nval = 1",
            "pkg/sub.py": "x = 1",
        },
    )
    result = analyze_map(tmp_path)
    # No self-referencing cycles should appear
    for cycle in result.cycles:
        nodes = cycle.path[:-1]
        assert not (len(nodes) == 1 and nodes[0].endswith("__init__.py"))


def test_hotspots_resolve_through_init(tmp_path: Path):
    """__init__.py facades should resolve to the actual modules behind them."""
    _make_project(
        tmp_path,
        {
            "core/__init__.py": "from core.models import M\nfrom core.utils import U",
            "core/models.py": "M = 'model'",
            "core/utils.py": "U = 'util'",
            "a.py": "from core import M",
            "b.py": "from core import M",
            "c.py": "from core import M",
            "d.py": "from core import M",
            "e.py": "from core import M",
        },
    )
    result = analyze_map(tmp_path, min_fan_in=3)
    hotspot_modules = [h.module for h in result.hotspots]
    # __init__.py should NOT be a hotspot
    assert not any("__init__.py" in m for m in hotspot_modules)
    # The actual modules behind __init__.py should be hotspots
    assert any("models.py" in m for m in hotspot_modules)
