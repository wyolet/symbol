"""Tests for TODO/FIXME scanner."""

from pathlib import Path

from ca.symbol.checkers.todos import detect, TodoItem
from ca.symbol.shared.ast_cache import ASTCache


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def _run(tmp_path: Path, cache: ASTCache) -> list[TodoItem]:
    results = []
    for f in cache.files:
        results.extend(detect(None, f))  # type: ignore[arg-type]
    return results


def test_finds_todo(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "x = 1  # TODO: fix this",
    })
    cache = ASTCache(tmp_path)
    todos = _run(tmp_path, cache)
    assert len(todos) == 1
    assert todos[0].tag == "TODO"
    assert todos[0].text == "fix this"
    assert todos[0].line == 1


def test_finds_fixme(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "# FIXME: broken logic\nx = 1",
    })
    cache = ASTCache(tmp_path)
    todos = _run(tmp_path, cache)
    assert len(todos) == 1
    assert todos[0].tag == "FIXME"


def test_finds_hack_and_xxx(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "# HACK: workaround\n# XXX: needs review",
    })
    cache = ASTCache(tmp_path)
    todos = _run(tmp_path, cache)
    assert len(todos) == 2
    tags = {t.tag for t in todos}
    assert tags == {"HACK", "XXX"}


def test_case_insensitive(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "# todo: lowercase\n# Todo: mixed",
    })
    cache = ASTCache(tmp_path)
    todos = _run(tmp_path, cache)
    assert len(todos) == 2
    assert all(t.tag == "TODO" for t in todos)


def test_no_todos(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "x = 1\n# just a comment",
    })
    cache = ASTCache(tmp_path)
    todos = _run(tmp_path, cache)
    assert len(todos) == 0


def test_fixme_and_todo_both_found(tmp_path: Path):
    _make_project(tmp_path, {
        "app.py": "# TODO: second\n# FIXME: first",
    })
    cache = ASTCache(tmp_path)
    todos = _run(tmp_path, cache)
    assert len(todos) == 2
    tags = {t.tag for t in todos}
    assert tags == {"TODO", "FIXME"}


def test_empty_project(tmp_path: Path):
    cache = ASTCache(tmp_path)
    todos = _run(tmp_path, cache)
    assert len(todos) == 0
