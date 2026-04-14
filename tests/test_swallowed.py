"""Tests for swallowed exception detection."""

from pathlib import Path

from ca_tools.shared.swallowed_finder import detect_swallowed
from ca_tools.shared.ast_cache import ASTCache


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def test_bare_except_pass(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "try:\n    x = 1\nexcept:\n    pass",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 1
    assert results[0].exception_type == "bare except"


def test_except_exception_pass(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "try:\n    x = 1\nexcept Exception:\n    pass",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 1
    assert results[0].exception_type == "Exception"


def test_except_ellipsis(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "try:\n    x = 1\nexcept ValueError:\n    ...",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 1
    assert results[0].exception_type == "ValueError"


def test_except_with_handler_not_flagged(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "try:\n    x = 1\nexcept Exception as e:\n    print(e)",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 0


def test_except_with_logging_not_flagged(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "try:\n    x = 1\nexcept Exception:\n    logger.error('failed')",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 0


def test_multiple_types(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "try:\n    x = 1\nexcept (TypeError, ValueError):\n    pass",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 1
    assert "TypeError" in results[0].exception_type
    assert "ValueError" in results[0].exception_type


def test_inside_function(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "def foo():\n    try:\n        x = 1\n    except:\n        pass",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 1
    assert results[0].context == "foo"


def test_inside_class(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "class Svc:\n    def run(self):\n        try:\n            x = 1\n        except:\n            pass",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 1
    assert results[0].context == "run"


def test_no_swallowed(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "x = 1\ndef foo(): pass",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 0


def test_docstring_only_is_swallowed(tmp_path: Path):
    """An except block with only a string is effectively swallowed."""
    _make_project(tmp_path, {
        "app.py": "try:\n    x = 1\nexcept:\n    'intentionally ignored'",
    })
    cache = ASTCache(tmp_path)
    results = detect_swallowed(tmp_path, cache)
    assert len(results) == 1
