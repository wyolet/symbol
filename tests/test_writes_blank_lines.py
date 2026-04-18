"""Tests for AST-aware blank-line normalization."""

from ca.symbol.writes._blank_lines import normalize_blank_gaps


def _norm(src: str) -> str:
    return normalize_blank_gaps(src.encode()).decode()


def test_pads_zero_blanks_to_two_at_module_level():
    src = "def foo():\n    return 1\ndef bar():\n    return 2\n"
    assert _norm(src) == "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"


def test_clamps_excess_blanks_to_two_at_module_level():
    src = "def foo():\n    return 1\n\n\n\n\n\ndef bar():\n    return 2\n"
    assert _norm(src) == "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"


def test_leaves_correct_spacing_alone():
    src = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
    assert _norm(src) == src


def test_class_methods_get_one_blank():
    src = "class C:\n    def a(self):\n        pass\n    def b(self):\n        pass\n"
    expected = "class C:\n    def a(self):\n        pass\n\n    def b(self):\n        pass\n"
    assert _norm(src) == expected


def test_class_methods_excess_clamped_to_one():
    src = "class C:\n    def a(self):\n        pass\n\n\n\n    def b(self):\n        pass\n"
    expected = "class C:\n    def a(self):\n        pass\n\n    def b(self):\n        pass\n"
    assert _norm(src) == expected


def test_decorator_stays_flush_with_def():
    """Blanks must go ABOVE the decorator, not between decorator and def."""
    src = (
        "def foo():\n    return 1\n"
        "@cache\ndef bar():\n    return 2\n"
    )
    out = _norm(src)
    # The @cache and def bar lines must remain adjacent.
    assert "@cache\ndef bar" in out
    # And there should be 2 blank lines between foo's body end and @cache.
    assert "return 1\n\n\n@cache" in out


def test_leading_comment_stays_with_def():
    """Blanks go above the comment block, not between comment and def."""
    src = (
        "def foo():\n    return 1\n"
        "# explains bar\ndef bar():\n    return 2\n"
    )
    out = _norm(src)
    assert "# explains bar\ndef bar" in out
    assert "return 1\n\n\n# explains bar" in out


def test_syntax_error_returns_unchanged():
    src = "def foo(:\n    broken\n"
    assert normalize_blank_gaps(src.encode()) == src.encode()


def test_first_statement_not_padded():
    """Module's first def has no prev sibling — should not get spurious blanks."""
    src = "def foo():\n    return 1\n"
    assert _norm(src) == src


def test_imports_get_two_blanks_before_def():
    """PEP 8 E305: 2 blank lines between top-level imports and a following def."""
    src = "import x\ndef foo():\n    return 1\n"
    expected = "import x\n\n\ndef foo():\n    return 1\n"
    assert _norm(src) == expected
