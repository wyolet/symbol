"""Tests for project-level configuration."""

from pathlib import Path

import pytest

from ca_tools.shared.findings import Severity
from ca_tools.shared.project_config import ProjectConfig, load_project_config


def test_defaults_when_no_pyproject(tmp_path: Path):
    config = load_project_config(tmp_path)
    assert config.include == []
    assert config.exclude == []
    assert config.checkers == {}
    assert config.packages == {}


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


def test_loads_checker_severity(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.checkers.orphans]
severity = "warning"

[tool.ca-tools.checkers.side_effects]
severity = "info"

[tool.ca-tools.checkers.unused_deps]
severity = "warning"
""")
    config = load_project_config(tmp_path)
    assert config.checkers["orphans"].severity == Severity.WARNING
    assert config.checkers["side_effects"].severity == Severity.INFO
    assert config.checkers["unused_deps"].severity == Severity.WARNING


def test_loads_checker_ignore(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.checkers.unused_deps]
ignore = ["greenlet", "psycopg"]

[tool.ca-tools.checkers.orphans]
ignore = ["alembic/*", "src/main.py"]

[tool.ca-tools.checkers.side_effects]
ignore = ["*.include_router()", "*.add_middleware()"]
""")
    config = load_project_config(tmp_path)
    assert config.checkers["unused_deps"].ignore == ["greenlet", "psycopg"]
    assert config.checkers["orphans"].ignore == ["alembic/*", "src/main.py"]
    assert config.checkers["side_effects"].ignore == ["*.include_router()", "*.add_middleware()"]


def test_invalid_severity_raises(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.checkers.orphans]
severity = "nonsense"
""")
    with pytest.raises(ValueError, match="Invalid severity"):
        load_project_config(tmp_path)


def test_partial_config(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.ca-tools.checkers.unused_deps]
ignore = ["greenlet"]
""")
    config = load_project_config(tmp_path)
    assert config.checkers["unused_deps"].ignore == ["greenlet"]
    assert config.exclude == []


def test_malformed_toml(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("this is not valid toml [[[")
    config = load_project_config(tmp_path)
    assert config == ProjectConfig()
