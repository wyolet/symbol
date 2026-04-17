"""Tests for config file detection."""

from pathlib import Path

from ca.symbol.shared.config_files import detect_config_files
from ca.symbol.shared.spec import load_spec

SPEC = load_spec()


def test_detects_dockerfile(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.13")
    configs = detect_config_files(tmp_path, SPEC)
    assert any(c.description == "containerized" for c in configs)


def test_detects_docker_compose(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text("version: '3'")
    configs = detect_config_files(tmp_path, SPEC)
    assert any(c.description == "multi-service" for c in configs)


def test_detects_env_file(tmp_path: Path):
    (tmp_path / ".env").write_text("SECRET=xxx")
    configs = detect_config_files(tmp_path, SPEC)
    assert any(c.description == "environment vars" for c in configs)


def test_detects_ci_dir(tmp_path: Path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("on: push")
    configs = detect_config_files(tmp_path, SPEC)
    assert any(c.description == "CI/CD" for c in configs)


def test_detects_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")
    configs = detect_config_files(tmp_path, SPEC)
    assert any(c.description == "project config" for c in configs)


def test_empty_project(tmp_path: Path):
    configs = detect_config_files(tmp_path, SPEC)
    assert len(configs) == 0
