"""Tests for custom checker discovery via [tool.symbol] custom_checkers."""

from pathlib import Path

import pytest

import wyolet.symbol.checkers  # noqa: F401 — ensure built-ins are registered first
from wyolet.symbol.shared.registry import _registry, clear, load_custom_checkers


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot and restore registry around each test."""
    before = dict(_registry)
    yield
    _registry.clear()
    _registry.update(before)


def test_load_custom_checker_registers_it(tmp_path: Path):
    checker_file = tmp_path / "my_checker.py"
    checker_file.write_text("""
from wyolet.symbol.shared.registry import register, views

@register(name="my_custom", description="test checker", kind="project", contributes_to_report=False, priority=999)
def detect(ctx):
    return ["found_something"]
""")

    loaded = load_custom_checkers(["my_checker.py"], tmp_path)

    assert loaded == ["my_checker.py"]
    assert "my_custom" in _registry
    assert _registry["my_custom"].info.description == "test checker"


def test_missing_file_warns(tmp_path: Path):
    with pytest.warns(UserWarning, match="not found"):
        loaded = load_custom_checkers(["nonexistent.py"], tmp_path)
    assert loaded == []


def test_broken_checker_warns(tmp_path: Path):
    checker_file = tmp_path / "broken.py"
    checker_file.write_text("raise RuntimeError('oops')")

    with pytest.warns(UserWarning, match="Failed to load"):
        loaded = load_custom_checkers(["broken.py"], tmp_path)
    assert loaded == []


def test_project_config_reads_custom_checkers(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.symbol]
custom_checkers = ["checkers/my_checker.py"]
""")
    from wyolet.symbol.shared.project_config import load_project_config
    config = load_project_config(tmp_path)
    assert config.custom_checkers == ["checkers/my_checker.py"]
