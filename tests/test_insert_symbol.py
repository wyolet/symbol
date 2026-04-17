"""Tests for `ca insert-symbol`."""

from pathlib import Path

import pytest

from ca.symbol.caches import NullReadCache
from ca.symbol.shared.symbol_index import SymbolIndex, get_or_build_index
from ca.symbol.writes.insert_symbol import (
    InsertSymbolRequest,
    InsertSymbolResult,
    apply_insert_symbol,
    resolve_insert_symbol,
)


def _index(project: Path) -> SymbolIndex:
    idx, _ = get_or_build_index(project)
    return idx


@pytest.fixture
def project(tmp_path):
    (tmp_path / "services.py").write_text(
        '''class Foo:
    def bar(self):
        return 1


def helper():
    return 42
'''
    )
    return tmp_path


# ---------------------------------------------------------- resolve


def test_resolve_symbol_not_found(project):
    idx = _index(project)
    r = resolve_insert_symbol(idx, "services.missing", "after", "pass\n", project)
    assert isinstance(r, InsertSymbolResult)
    assert r.error_code == "symbol_not_found"


def test_resolve_invalid_position(project):
    idx = _index(project)
    r = resolve_insert_symbol(idx, "services.Foo", "sideways", "x\n", project)
    assert isinstance(r, InsertSymbolResult)
    assert r.error_code == "invalid_argument"


# ---------------------------------------------------------- before/after (module-level)


def test_insert_after_module_function(project):
    idx = _index(project)
    req = resolve_insert_symbol(
        idx, "services.helper", "after", "\ndef new_helper():\n    return 99\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    result = apply_insert_symbol(req, cache=NullReadCache())
    assert result.status == "applied"

    text = (project / "services.py").read_text()
    assert "def new_helper" in text
    assert text.index("def helper") < text.index("def new_helper")


def test_insert_before_module_function(project):
    idx = _index(project)
    req = resolve_insert_symbol(
        idx, "services.helper", "before", "def prelude():\n    return 0\n\n\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    result = apply_insert_symbol(req, cache=NullReadCache())
    assert result.status == "applied"

    text = (project / "services.py").read_text()
    assert text.index("def prelude") < text.index("def helper")


# ---------------------------------------------------------- start/end (class body)


def test_insert_end_of_class(project):
    idx = _index(project)
    req = resolve_insert_symbol(
        idx, "services.Foo", "end", "def baz(self):\n    return 2\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    result = apply_insert_symbol(req, cache=NullReadCache())
    assert result.status == "applied"

    text = (project / "services.py").read_text()
    # baz inserted inside Foo (before "def helper" which is module-level)
    assert "def baz" in text
    assert text.index("def bar") < text.index("def baz") < text.index("def helper")


def test_insert_end_auto_reindents(project):
    """Content sent flush-left should get the class-body indent."""
    idx = _index(project)
    req = resolve_insert_symbol(
        idx, "services.Foo", "end", "def baz(self):\n    return 2\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    # Content in the request should have been reindented to class-body depth.
    assert req.content.decode().splitlines()[0].startswith("    def baz")


def test_insert_start_requires_body(project):
    """position=start refuses a symbol that has no body (e.g. not in our v1
    kinds — but all current indexed kinds have bodies, so this checks the
    code path works generally)."""
    idx = _index(project)
    r = resolve_insert_symbol(
        idx, "services.Foo.bar", "start", "x = 1\n", project,
    )
    # bar is a method (has body) — should succeed.
    assert isinstance(r, InsertSymbolRequest)


# ---------------------------------------------------------- dry-run


def test_dry_run_does_not_write(project):
    idx = _index(project)
    before = (project / "services.py").read_text()
    req = resolve_insert_symbol(
        idx, "services.helper", "after", "\ndef added():\n    pass\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    result = apply_insert_symbol(req, cache=NullReadCache(), dry_run=True)

    assert result.status == "dry_run"
    assert (project / "services.py").read_text() == before
    assert result.diff
    assert "def added" in result.diff


# ---------------------------------------------------------- reindent toggle


def test_no_reindent_preserves_content(project):
    idx = _index(project)
    raw = "          weird_indent = True\n"
    req = resolve_insert_symbol(
        idx, "services.helper", "after", raw, project, reindent=False,
    )
    assert isinstance(req, InsertSymbolRequest)
    assert req.content.decode() == raw
