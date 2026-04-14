"""Tests for LOC counter."""

from pathlib import Path

from ca_tools.shared.loc_counter import _count_file, _detect_language, count_loc


def test_detect_language_by_ext(tmp_path: Path):
    assert _detect_language(tmp_path / "app.py") == "Python"
    assert _detect_language(tmp_path / "index.ts") == "TypeScript"
    assert _detect_language(tmp_path / "main.go") == "Go"
    assert _detect_language(tmp_path / "unknown.xyz") is None


def test_detect_language_named_files(tmp_path: Path):
    assert _detect_language(tmp_path / "Dockerfile") == "Dockerfile"
    assert _detect_language(tmp_path / "Makefile") == "Makefile"


def test_count_python_file():
    source = """# This is a comment
import os

def main():
    # inline comment
    print("hello")

\"\"\"
Docstring block
\"\"\"
"""
    stats = _count_file(source, ".py")
    assert stats.lines == 10
    assert stats.blanks == 2
    assert stats.comments >= 2
    assert stats.code >= 3


def test_count_empty_file():
    stats = _count_file("", ".py")
    assert stats.lines == 0
    assert stats.code == 0
    assert stats.blanks == 0
    assert stats.comments == 0


def test_count_loc_project(tmp_path: Path):
    (tmp_path / "app.py").write_text("# comment\nimport os\n\nprint('hi')\n")
    (tmp_path / "config.yaml").write_text("# yaml comment\nkey: value\n")
    (tmp_path / "README.md").write_text("# Title\n\nSome text.\n")
    result = count_loc(tmp_path)
    assert result.total_files == 3
    assert result.total_lines > 0
    assert "Python" in result.by_language
    assert "YAML" in result.by_language


def test_skips_venv(tmp_path: Path):
    venv = tmp_path / "venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "pkg.py").write_text("x = 1\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    result = count_loc(tmp_path)
    assert result.total_files == 1


def test_lang_stats_code_pct():
    from ca_tools.shared.loc_counter import LangStats

    ls = LangStats(language="Python", files=1, lines=100, code=80, blanks=10, comments=10)
    assert ls.code_pct == 80.0

    empty = LangStats(language="Python")
    assert empty.code_pct == 0.0
