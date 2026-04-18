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
def test_insert_before_decorated_function(tmp_path):
    """Regression: insert 'before' a decorated function must land ABOVE the
    decorator, not between the decorator and `def` (which would silently
    re-bind the decorator to the inserted function).
    """
    (tmp_path / "services.py").write_text(
        "import functools\n"
        "\n"
        "\n"
        "@functools.lru_cache\n"
        "def cached_op():\n"
        "    return 1\n"
    )
    idx, _ = get_or_build_index(tmp_path)
    req = resolve_insert_symbol(
        idx, "services.cached_op", "before",
        "def prelude():\n    return 0\n\n\n",
        tmp_path,
    )
    assert isinstance(req, InsertSymbolRequest)
    result = apply_insert_symbol(req, cache=NullReadCache())
    assert result.status == "applied"

    text = (tmp_path / "services.py").read_text()
    # The decorator must still immediately precede `def cached_op`.
    cached_def_line = text.index("def cached_op")
    decorator_line = text.index("@functools.lru_cache")
    prelude_line = text.index("def prelude")
    # prelude lands above the decorator, decorator immediately above def.
    assert prelude_line < decorator_line < cached_def_line
    # No code between decorator and def cached_op (only whitespace allowed).
    between = text[text.index("@functools.lru_cache") : cached_def_line]
    assert between.strip() == "@functools.lru_cache"


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
    non_blank = [ln for ln in req.content.decode().splitlines() if ln.strip()]
    assert non_blank[0].startswith("    def baz")


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
def test_insert_after_pads_two_blank_lines_top_level(project):
    """Regression: agents may forget to prefix '\\n\\n' on `after` inserts at
    module scope. The resolver should auto-pad to PEP 8 spacing."""
    idx = _index(project)
    req = resolve_insert_symbol(
        idx, "services.helper", "after", "def added():\n    return 1\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    apply_insert_symbol(req, cache=NullReadCache())

    text = (project / "services.py").read_text()
    # Two blank lines must separate `helper` from `added`.
    assert "return 42\n\n\ndef added" in text


def test_insert_end_of_class_pads_one_blank_line(project):
    idx = _index(project)
    req = resolve_insert_symbol(
        idx, "services.Foo", "end", "def baz(self):\n    return 2\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    apply_insert_symbol(req, cache=NullReadCache())

    text = (project / "services.py").read_text()
    # One blank line between `bar` body end and the new `baz`.
    assert "return 1\n\n    def baz" in text


def test_insert_pad_normalizes_redundant_leading_blanks(project):
    """If the agent does prefix '\\n\\n', we must not double-pad to 4 blanks."""
    idx = _index(project)
    req = resolve_insert_symbol(
        idx, "services.helper", "after", "\n\n\ndef added():\n    return 1\n", project,
    )
    assert isinstance(req, InsertSymbolRequest)
    apply_insert_symbol(req, cache=NullReadCache())

    text = (project / "services.py").read_text()
    assert "return 42\n\n\ndef added" in text
    assert "return 42\n\n\n\ndef added" not in text


def test_insert_no_reindent_skips_padding(project):
    """reindent=False is escape hatch: content passes through verbatim."""
    idx = _index(project)
    raw = "def added():\n    return 1\n"
    req = resolve_insert_symbol(
        idx, "services.helper", "after", raw, project, reindent=False,
    )
    assert isinstance(req, InsertSymbolRequest)
    assert req.content.decode() == raw
