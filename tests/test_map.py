"""Tests for import map analysis."""

from pathlib import Path

from wyolet.symbol.shared.graph import analyze_map
from wyolet.symbol.shared.project_config import MapThresholds, MetricThreshold


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def _thresholds(**overrides) -> MapThresholds:
    """Create thresholds with low defaults for testing."""
    t = MapThresholds()
    for key, val in overrides.items():
        setattr(t, key, val)
    return t


def test_detects_cycle(tmp_path: Path):
    """Real circular import: a imports b, b imports a, both need names not yet defined."""
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x\ny = 1",
            "b.py": "from a import y\nx = 1",
        },
    )
    result = analyze_map(tmp_path)
    # The simulator checks if names are available — both x and y are defined
    # after the import line, so this IS a real cycle
    assert len(result.cycles) >= 1
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
    thresholds = _thresholds(hotspots=MetricThreshold(info=3, warning=5, error=10))
    result = analyze_map(tmp_path, thresholds=thresholds)
    assert len(result.hotspots) == 1
    assert result.hotspots[0].module == "core.py"
    assert result.hotspots[0].fan_in == 3


def test_fragile(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "m1.py": "x = 1",
            "m2.py": "x = 2",
            "m3.py": "x = 3",
            "main.py": "from m1 import x\nfrom m2 import x\nfrom m3 import x",
        },
    )
    thresholds = _thresholds(fragile=MetricThreshold(info=3, warning=5, error=10))
    result = analyze_map(tmp_path, thresholds=thresholds)
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
    # util.py is only imported by main.py — it's a leaf (small file)
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
    thresholds = _thresholds(deep_chains=MetricThreshold(info=4, warning=6, error=10))
    result = analyze_map(tmp_path, thresholds=thresholds)
    assert len(result.deep_chains) >= 1
    assert len(result.deep_chains[0].chain) == 4


def test_deep_chains_filtered(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x",
            "b.py": "from c import x\nx = 1",
            "c.py": "x = 1",
        },
    )
    thresholds = _thresholds(deep_chains=MetricThreshold(info=5, warning=7, error=10))
    result = analyze_map(tmp_path, thresholds=thresholds)
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
            "pkg/__init__.py": "from .models import Model\nfrom .utils import helper",
            "pkg/models.py": "Model = 'model'",
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
            "pkg_a/__init__.py": "from pkg_b import x\ny = 1",
            "pkg_b/__init__.py": "from pkg_a import y\nx = 1",
        },
    )
    result = analyze_map(tmp_path)
    assert len(result.cycles) >= 1


def test_type_checking_imports_not_edges(tmp_path: Path):
    """Imports under TYPE_CHECKING should not create edges."""
    _make_project(
        tmp_path,
        {
            "a.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from b import x\n",
            "b.py": "x = 1",
        },
    )
    result = analyze_map(tmp_path)
    assert result.total_edges == 0


def test_submodule_fallback_no_false_cycle(tmp_path: Path):
    """from . import sub where sub is a submodule file should not create edge to __init__."""
    _make_project(
        tmp_path,
        {
            "pkg/__init__.py": "from .a import ClassA",
            "pkg/a.py": "from . import b\nClassA = 'a'",
            "pkg/b.py": "x = 1",
        },
    )
    result = analyze_map(tmp_path)
    assert len(result.cycles) == 0


def test_coupling(tmp_path: Path):
    """Module coupling detects cross-package dependencies."""
    _make_project(
        tmp_path,
        {
            "api/routes.py": "from models.user import User",
            "models/user.py": "User = 'user'",
        },
    )
    result = analyze_map(tmp_path)
    assert len(result.coupling) > 0
    api_node = next((n for n in result.coupling if n.name == "api"), None)
    assert api_node is not None
    assert "models" in api_node.deps


def test_mutual_coupling(tmp_path: Path):
    """Bidirectional package dependency is flagged as mutual."""
    _make_project(
        tmp_path,
        {
            "api/routes.py": "from services.auth import login",
            "services/auth.py": "from api.helpers import validate\nlogin = 1",
            "api/helpers.py": "validate = 1",
        },
    )
    result = analyze_map(tmp_path)
    mutual = [n for n in result.coupling if n.mutual]
    assert len(mutual) > 0
