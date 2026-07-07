"""Tests for class-base capture: adapter scan + renamer resolution."""

from pathlib import Path

from wyolet.symbol.adapters.python_ast import PythonAstAdapter
from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.rename.index_query import RenamerIndexQuery


def _bases_by_name(source: str) -> dict[str, tuple[str, ...]]:
    """Map every top-level symbol name to its captured bases tuple."""
    adapter = PythonAstAdapter()
    scan = adapter.scan_file(
        Path("m.py"), source.encode("utf-8"), module_prefix="",
    )
    assert scan.ok
    return {s.name: s.bases for s in scan.symbols}


# ---------------------------------------------------------- adapter capture


def test_simple_base():
    assert _bases_by_name("class A(Base): pass")["A"] == ("Base",)


def test_dotted_base():
    assert _bases_by_name("class A(mod.Base): pass")["A"] == ("mod.Base",)


def test_subscript_base_unwrapped():
    # Generic[T] -> Generic (the subscript value only).
    assert _bases_by_name("class A(Generic[T]): pass")["A"] == ("Generic",)


def test_multiple_bases():
    assert _bases_by_name("class A(One, Two, three.Four): pass")["A"] == (
        "One",
        "Two",
        "three.Four",
    )


def test_keyword_bases_ignored():
    # metaclass= and other keyword args are not positional bases.
    bases = _bases_by_name("class A(Base, metaclass=Meta): pass")["A"]
    assert bases == ("Base",)


def test_no_bases():
    assert _bases_by_name("class A: pass")["A"] == ()


def test_non_class_has_no_bases():
    assert _bases_by_name("def f(): return 1")["f"] == ()


# ---------------------------------------------------------- renamer resolution


def test_class_bases_resolves_across_import(tmp_path):
    (tmp_path / "base.py").write_text("class Base:\n    pass\n")
    (tmp_path / "sub.py").write_text(
        "from base import Base\n\n\nclass Sub(Base):\n    pass\n"
    )
    idx, _ = get_or_build_index(tmp_path)
    iq = RenamerIndexQuery(idx, tmp_path)
    assert iq.class_bases("sub.Sub") == ("base.Base",)


def test_class_bases_resolves_same_module_sibling(tmp_path):
    (tmp_path / "pkg.py").write_text(
        "class Base:\n    pass\n\n\nclass Sub(Base):\n    pass\n"
    )
    idx, _ = get_or_build_index(tmp_path)
    iq = RenamerIndexQuery(idx, tmp_path)
    assert iq.class_bases("pkg.Sub") == ("pkg.Base",)


def test_class_bases_drops_unresolvable(tmp_path):
    # `object` isn't declared or imported in-project -> dropped.
    (tmp_path / "pkg.py").write_text("class Sub(object):\n    pass\n")
    idx, _ = get_or_build_index(tmp_path)
    iq = RenamerIndexQuery(idx, tmp_path)
    assert iq.class_bases("pkg.Sub") == ()
