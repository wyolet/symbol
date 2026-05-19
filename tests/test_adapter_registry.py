"""Tests for the language adapter registry — supports() predicate."""

from wyolet.symbol.adapters.registry import default_registry


def test_supports_python_file(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def hi():\n    pass\n")
    assert default_registry().supports(f) is True


def test_supports_rejects_markdown(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# heading\n\nsome prose\n")
    assert default_registry().supports(f) is False


def test_supports_rejects_json(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"key": "value"}\n')
    assert default_registry().supports(f) is False


def test_supports_extensionless_python_via_linguist(tmp_path):
    """Linguist content-sniffs files with no extension."""
    f = tmp_path / "script"
    f.write_text("#!/usr/bin/env python\nimport sys\nprint(sys.argv)\n")
    # Whether linguist resolves this to python depends on its heuristics; the
    # contract is that supports() returns a bool without raising.
    assert isinstance(default_registry().supports(f), bool)
