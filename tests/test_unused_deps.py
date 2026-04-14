"""Tests for unused dependency detection."""

from pathlib import Path

from ca_tools.shared.unused_dep_finder import detect_unused_deps
from ca_tools.shared.spec import load_spec

SPEC = load_spec()


def test_used_dep_not_flagged(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("import requests\nrequests.get('http://example.com')")
    unused = detect_unused_deps(tmp_path, ["requests"], SPEC)
    assert unused == []


def test_unused_dep_flagged(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("import flask\n")
    unused = detect_unused_deps(tmp_path, ["flask", "celery"], SPEC)
    assert "celery" in unused
    assert "flask" not in unused


def test_import_name_mapping(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("from PIL import Image\n")
    unused = detect_unused_deps(tmp_path, ["pillow"], SPEC)
    assert unused == []


def test_import_name_mapping_yaml(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("import yaml\n")
    unused = detect_unused_deps(tmp_path, ["pyyaml"], SPEC)
    assert unused == []


def test_from_import(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("from sqlalchemy import create_engine\n")
    unused = detect_unused_deps(tmp_path, ["sqlalchemy"], SPEC)
    assert unused == []


def test_no_python_files(tmp_path: Path):
    unused = detect_unused_deps(tmp_path, ["requests"], SPEC)
    assert unused == ["requests"]


def test_exclude_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "vendor").mkdir()
    (tmp_path / "src" / "app.py").write_text("import flask\n")
    (tmp_path / "vendor" / "lib.py").write_text("import celery\n")
    # With vendor excluded, celery should appear unused
    unused = detect_unused_deps(tmp_path, ["flask", "celery"], SPEC, exclude=["vendor/*"])
    assert "flask" not in unused
    assert "celery" in unused
