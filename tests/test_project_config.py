"""Tests for project-level configuration."""

from pathlib import Path

import pytest

from ca_tools.shared.findings import Severity
from ca_tools.shared.project_config import ProjectConfig, load_project_config


def test_defaults_when_no_pyproject(tmp_path: Path):
    config = load_project_config(tmp_path)
    assert config.include == []
    assert config.exclude == []
    assert config.severity_orphans == Severity.ERROR
    assert config.severity_side_effects == Severity.WARNING
    assert config.severity_unused_deps == Severity.ERROR
    assert config.ignore_deps == []
    assert config.ignore_orphans == []
    assert config.ignore_side_effects == []


def test_defaults_when_no_ca_tools_section(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    config = load_project_config(tmp_path)
    assert config == ProjectConfig()


def test_loads_include_exclude(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools]
include = ["src/*"]
exclude = ["tests/*", "scripts/*"]
""")
    config = load_project_config(tmp_path)
    assert config.include == ["src/*"]
    assert config.exclude == ["tests/*", "scripts/*"]


def test_loads_severity_overrides(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.severity]
orphans = "warning"
side_effects = "info"
unused_deps = "warning"
""")
    config = load_project_config(tmp_path)
    assert config.severity_orphans == Severity.WARNING
    assert config.severity_side_effects == Severity.INFO
    assert config.severity_unused_deps == Severity.WARNING


def test_loads_ignore_lists(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.ignore]
deps = ["greenlet", "psycopg"]
orphans = ["alembic/*", "src/main.py"]
side_effects = ["*.include_router()", "*.add_middleware()"]
""")
    config = load_project_config(tmp_path)
    assert config.ignore_deps == ["greenlet", "psycopg"]
    assert config.ignore_orphans == ["alembic/*", "src/main.py"]
    assert config.ignore_side_effects == ["*.include_router()", "*.add_middleware()"]


def test_invalid_severity_raises(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.severity]
orphans = "critical"
""")
    with pytest.raises(ValueError, match="Invalid severity"):
        load_project_config(tmp_path)


def test_partial_config(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.ignore]
deps = ["greenlet"]
""")
    config = load_project_config(tmp_path)
    assert config.ignore_deps == ["greenlet"]
    # Everything else stays default
    assert config.severity_orphans == Severity.ERROR
    assert config.exclude == []


def test_malformed_toml(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("this is not valid toml [[[")
    config = load_project_config(tmp_path)
    assert config == ProjectConfig()
