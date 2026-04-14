"""Tests for unused dependency detection."""

from pathlib import Path

import ca_tools.checkers  # noqa: F401
from ca_tools.checkers.unused_deps import detect
from ca_tools.shared.context import build_context


def _make_pyproject(tmp_path: Path, deps: list[str]) -> None:
    items = ", ".join(f'"{d}"' for d in deps)
    (tmp_path / "pyproject.toml").write_text(f"[project]\nname = 'test'\ndependencies = [{items}]\n")


def _run(tmp_path: Path, deps: list[str], **kwargs) -> list[str]:
    _make_pyproject(tmp_path, deps)
    ctx = build_context(tmp_path, **kwargs)
    return detect(ctx)


def test_used_dep_not_flagged(tmp_path: Path):
    (tmp_path / "app.py").write_text("import requests\nrequests.get('http://example.com')")
    unused = _run(tmp_path, ["requests"])
    assert unused == []


def test_unused_dep_flagged(tmp_path: Path):
    (tmp_path / "app.py").write_text("import flask\n")
    unused = _run(tmp_path, ["flask", "celery"])
    assert "celery" in unused
    assert "flask" not in unused


def test_import_name_mapping(tmp_path: Path):
    (tmp_path / "app.py").write_text("from PIL import Image\n")
    unused = _run(tmp_path, ["pillow"])
    assert unused == []


def test_import_name_mapping_yaml(tmp_path: Path):
    (tmp_path / "app.py").write_text("import yaml\n")
    unused = _run(tmp_path, ["pyyaml"])
    assert unused == []


def test_from_import(tmp_path: Path):
    (tmp_path / "app.py").write_text("from sqlalchemy import create_engine\n")
    unused = _run(tmp_path, ["sqlalchemy"])
    assert unused == []


def test_no_python_files(tmp_path: Path):
    unused = _run(tmp_path, ["requests"])
    assert unused == ["requests"]


def test_exclude_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "vendor").mkdir()
    (tmp_path / "src" / "app.py").write_text("import flask\n")
    (tmp_path / "vendor" / "lib.py").write_text("import celery\n")
    unused = _run(tmp_path, ["flask", "celery"], exclude=["vendor/*"])
    assert "flask" not in unused
    assert "celery" in unused
