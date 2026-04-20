"""Tests for the shared write-content normalizer."""

from wyolet.symbol.writes._content import normalize_content


def test_strips_leading_trailing_blanks():
    out = normalize_content("\n\ndef foo():\n    return 1\n\n\n", "")
    assert out == b"def foo():\n    return 1\n"


def test_dedents_uniform_extra_indent():
    raw = "        def foo():\n            return 1\n"
    assert normalize_content(raw, "") == b"def foo():\n    return 1\n"


def test_reindents_to_target():
    raw = "def foo():\n    return 1\n"
    assert normalize_content(raw, "    ") == b"    def foo():\n        return 1\n"


def test_first_line_extra_whitespace_does_not_poison():
    """Min-indent across non-blank lines is what matters, not just first line."""
    raw = "    def foo():\n    return 1\n"
    assert normalize_content(raw, "") == b"def foo():\nreturn 1\n"


def test_blank_interior_lines_stay_blank():
    raw = "def foo():\n    x = 1\n\n    return x\n"
    out = normalize_content(raw, "")
    assert out == b"def foo():\n    x = 1\n\n    return x\n"


def test_empty_input_returns_empty():
    assert normalize_content("", "") == b""
    assert normalize_content("\n\n\n", "") == b""


def test_appends_trailing_newline():
    assert normalize_content("x = 1", "") == b"x = 1\n"


def test_bytes_input():
    assert normalize_content(b"def foo():\n    pass\n", "") == b"def foo():\n    pass\n"
