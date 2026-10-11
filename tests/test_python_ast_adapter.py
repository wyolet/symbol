"""PythonAstAdapter scan output (class bases, constants, fields) and the
relative-import resolution rule its rename analysis uses."""

import ast
from pathlib import Path

import pytest

from wyolet.symbol.adapters._python_ast_rename import _from_module
from wyolet.symbol.adapters.python_ast import PythonAstAdapter


def _scan(source: str):
    scan = PythonAstAdapter().scan_file(Path("m.py"), source.encode("utf-8"), module_prefix="m")
    assert scan.ok
    return scan


def _bases_by_name(source: str) -> dict[str, tuple[str, ...]]:
    return {s.name: s.bases for s in _scan(source).symbols}


def _scan_kinds(source: str) -> dict[str, str]:
    """qpath -> kind for every scanned symbol, recursively."""
    out: dict[str, str] = {}

    def walk(syms):
        for s in syms:
            out[s.qualified_path] = s.kind
            walk(s.children)

    walk(_scan(source).symbols)
    return out


# ── class bases ─────────────────────────────────────────────────────


def test_simple_base():
    assert _bases_by_name("class A(Base): pass")["A"] == ("Base",)


def test_dotted_base():
    assert _bases_by_name("class A(mod.Base): pass")["A"] == ("mod.Base",)


def test_subscript_base_unwrapped():
    # Generic[T] -> Generic (the subscript value only).
    assert _bases_by_name("class A(Generic[T]): pass")["A"] == ("Generic",)


def test_multiple_bases():
    assert _bases_by_name("class A(One, Two, three.Four): pass")["A"] == ("One", "Two", "three.Four")


def test_keyword_bases_ignored():
    # metaclass= and other keyword args are not positional bases.
    assert _bases_by_name("class A(Base, metaclass=Meta): pass")["A"] == ("Base",)


def test_no_bases():
    assert _bases_by_name("class A: pass")["A"] == ()


def test_non_class_has_no_bases():
    assert _bases_by_name("def f(): return 1")["f"] == ()


# ── constants ───────────────────────────────────────────────────────


def test_upper_case_module_assigns_are_constants():
    kinds = _scan_kinds("COLOR = 'red'\nMAX_SIZE: int = 10\nlower = 1\nMixedCase = 2\n")
    assert kinds.get("m.COLOR") == "constant"
    assert kinds.get("m.MAX_SIZE") == "constant"
    assert "m.lower" not in kinds
    assert "m.MixedCase" not in kinds


def test_constants_only_at_module_level():
    kinds = _scan_kinds("def f():\n    LOCAL = 1\n    return LOCAL\n")
    assert "m.f.LOCAL" not in kinds
    assert "m.f" in kinds


# ── fields ──────────────────────────────────────────────────────────


def test_class_body_and_init_fields():
    kinds = _scan_kinds(
        "class C:\n"
        "    field_a: int = 0\n"
        "    field_b = 'x'\n"
        "    def __init__(self):\n"
        "        self.count = 0\n"
        "        local_var = 3\n"
        "        return local_var\n"
        "    def method(self):\n"
        "        self.late = 9\n"
    )
    assert kinds.get("m.C.field_a") == "field"
    assert kinds.get("m.C.field_b") == "field"
    assert kinds.get("m.C.count") == "field"
    assert "m.C.local_var" not in kinds
    assert "m.C.late" not in kinds  # self.x outside __init__
    assert kinds.get("m.C.method") == "method"


def test_field_declared_twice_is_one_row_on_the_class_body():
    scan = _scan("class C:\n    count: int = 0\n    def __init__(self):\n        self.count = 5\n")
    fields = [c for cls in scan.symbols for c in cls.children if c.kind == "field"]
    assert [f.qualified_path for f in fields] == ["m.C.count"]
    assert fields[0].line_range[0] == 2


def test_no_fields_for_module_or_locals():
    kinds = _scan_kinds("x = 1\ndef f():\n    y = 2\n    return y\n")
    assert all(k != "field" for k in kinds.values())


# ── relative imports ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stmt", "module_prefix", "is_package", "expected"),
    [
        ("from x import y", "pkg.mod", False, "x"),
        ("from .helpers import y", "pkg.mod", False, "pkg.helpers"),
        ("from . import y", "pkg.mod", False, "pkg"),
        ("from ..helpers import y", "pkg.sub.mod", False, "pkg.helpers"),
        ("from .. import y", "pkg.sub.mod", False, "pkg"),
        ("from ...a.b import y", "pkg.sub.deep.mod", False, "pkg.a.b"),
        ("from .helpers import y", "pkg", True, "pkg.helpers"),
        ("from . import y", "pkg", True, "pkg"),
        ("from ..helpers import y", "pkg.sub", True, "pkg.helpers"),
        ("from .. import y", "pkg.sub", True, "pkg"),
        ("from ... import y", "pkg.mod", False, ""),
    ],
)
def test_from_module_resolves_relative_levels(stmt, module_prefix, is_package, expected):
    assert _from_module(ast.parse(stmt).body[0], module_prefix, is_package) == expected
