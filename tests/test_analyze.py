"""Tests for per-file analysis — exports, CC, depth, usage."""

from pathlib import Path

from ca_tools.analyze.analyzer import analyze_all, analyze_file


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def test_basic_exports(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "def foo(): pass\nclass Bar: pass\nX = 1",
    })
    result = analyze_file(tmp_path, "app.py")
    assert result is not None
    names = {e.name for e in result.exports}
    assert "foo" in names
    assert "Bar" in names


def test_cyclomatic_complexity(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "def simple(): pass\ndef complex_fn(x):\n    if x > 0:\n        for i in range(x):\n            if i % 2:\n                pass",
    })
    result = analyze_file(tmp_path, "app.py")
    exports = {e.name: e for e in result.exports}
    assert exports["simple"].complexity == 1
    assert exports["complex_fn"].complexity > 1


def test_max_depth(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "def deep():\n    if True:\n        for x in []:\n            if x:\n                pass",
    })
    result = analyze_file(tmp_path, "app.py")
    exports = {e.name: e for e in result.exports}
    assert exports["deep"].max_depth == 3


def test_external_usage(tmp_path: Path):
    _make_project(tmp_path, {
        "lib.py": "def helper(): pass",
        "app.py": "from lib import helper",
    })
    result = analyze_file(tmp_path, "lib.py")
    exports = {e.name: e for e in result.exports}
    assert "app.py" in exports["helper"].used_by


def test_internal_refs(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "class Config: pass\nsettings = Config()",
    })
    result = analyze_file(tmp_path, "app.py")
    exports = {e.name: e for e in result.exports}
    assert exports["Config"].internal_refs > 0


def test_init_not_counted_as_importer(tmp_path: Path):
    """Ancestor __init__.py re-exports should not count as importers."""
    _make_project(tmp_path, {
        "pkg/__init__.py": "from .mod import Foo",
        "pkg/mod.py": "Foo = 'foo'",
        "app.py": "from pkg.mod import Foo",
    })
    result = analyze_file(tmp_path, "pkg/mod.py")
    exports = {e.name: e for e in result.exports}
    # Only app.py, not __init__.py
    assert len(exports["Foo"].used_by) == 1
    assert "app.py" in exports["Foo"].used_by


def test_blast_radius(tmp_path: Path):
    _make_project(tmp_path, {
        "base.py": "x = 1",
        "a.py": "from base import x",
        "b.py": "from a import x\nx = 1",
    })
    result = analyze_file(tmp_path, "base.py")
    assert result.direct_importers == 1  # a.py
    assert result.transitive_importers == 1  # b.py


def test_class_methods(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "class Svc:\n    def simple(self): pass\n    def complex(self, x):\n        if x:\n            for i in range(x):\n                if i: pass",
    })
    result = analyze_file(tmp_path, "app.py")
    svc = next(e for e in result.exports if e.name == "Svc")
    assert len(svc.methods) == 2
    method_names = {m.name for m in svc.methods}
    assert "simple" in method_names
    assert "complex" in method_names
    # Methods sorted by CC descending
    assert svc.methods[0].complexity > svc.methods[1].complexity


def test_hidden_imports(tmp_path: Path):
    """Deferred and TYPE_CHECKING imports should be tracked."""
    _make_project(tmp_path, {
        "app.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from b import x\ndef foo():\n    from c import y",
        "b.py": "x = 1",
        "c.py": "y = 1",
    })
    result = analyze_file(tmp_path, "app.py")
    scopes = {i.scope for i in result.imports}
    assert "type_checking" in scopes
    assert "deferred" in scopes


def test_analyze_all(tmp_path: Path):
    _make_project(tmp_path, {
        "a.py": "x = 1",
        "b.py": "from a import x",
    })
    results = analyze_all(tmp_path)
    assert len(results) == 2
    paths = {r.path for r in results}
    assert "a.py" in paths
    assert "b.py" in paths


def test_file_not_found(tmp_path: Path):
    _make_project(tmp_path, {"a.py": "x = 1"})
    result = analyze_file(tmp_path, "nonexistent.py")
    assert result is None


def test_variables_not_in_exports_when_unused(tmp_path: Path):
    """Private variables (starting with _) should not appear in exports."""
    _make_project(tmp_path, {
        "app.py": "_private = 1\npublic = 2",
    })
    result = analyze_file(tmp_path, "app.py")
    names = {e.name for e in result.exports}
    assert "_private" not in names
    assert "public" in names
