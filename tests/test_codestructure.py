"""Tests for code structure analysis — functions, classes, type hints."""

from pathlib import Path

from ca_tools.shared.codestructure_finder import detect_code_structure
from ca_tools.shared.ast_cache import ASTCache


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def test_counts_functions_and_classes(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "def foo(): pass\ndef bar(): pass\nclass Baz: pass",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.functions == 2
    assert cs.classes == 1
    assert cs.methods == 0


def test_counts_methods(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "class Foo:\n    def bar(self): pass\n    def baz(self): pass",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.classes == 1
    assert cs.methods == 2
    assert cs.functions == 0


def test_typed_functions(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "def typed() -> int: return 1\ndef untyped(): return 1",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.typed_functions == 1
    assert cs.total_callables == 2
    assert cs.type_coverage_pct == 50.0


def test_typed_args(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "def foo(x: int, y, z: str): pass",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.typed_args == 2
    assert cs.total_args == 3


def test_self_cls_excluded_from_args(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "class Foo:\n    def bar(self, x: int): pass\n    @classmethod\n    def baz(cls, y): pass",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    # self and cls should not count
    assert cs.total_args == 2
    assert cs.typed_args == 1


def test_class_attrs(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "class Foo:\n    name: str = 'x'\n    age: int = 0\n    data = {}",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.typed_attrs == 2
    assert cs.total_attrs == 3


def test_module_vars(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "x: int = 1\ny = 2\nz: str = 'hello'",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.typed_vars == 2
    assert cs.total_vars == 3


def test_empty_project(tmp_path: Path):
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.total_callables == 0
    assert cs.type_coverage_pct == 0.0


def test_async_functions(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "async def fetch() -> str: return ''\ndef sync(): pass",
    })
    cache = ASTCache(tmp_path)
    cs = detect_code_structure(tmp_path, cache)
    assert cs.functions == 2
    assert cs.typed_functions == 1
