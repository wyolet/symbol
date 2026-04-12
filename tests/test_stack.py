"""Tests for stack detection."""

from pathlib import Path

from ca_tools.audit.stack import _extract_package_name, detect_deps, detect_stack, parse_pyproject_toml
from ca_tools.shared.spec import load_spec

SPEC = load_spec()


def test_extract_package_name():
    assert _extract_package_name("requests>=2.0") == "requests"
    assert _extract_package_name("flask[async]>=2.0") == "flask"
    assert _extract_package_name("django~=4.0") == "django"
    assert _extract_package_name("  numpy  ") == "numpy"
    assert _extract_package_name("") is None


def test_parse_pyproject_toml(tmp_path: Path):
    toml = tmp_path / "pyproject.toml"
    toml.write_text("""
[project]
dependencies = [
    "fastapi>=0.100",
    "sqlalchemy[asyncio]>=2.0",
    "redis>=5.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]
""")
    deps = parse_pyproject_toml(toml)
    assert "fastapi" in deps
    assert "sqlalchemy" in deps
    assert "redis" in deps
    assert "pytest" in deps


def test_detect_stack(tmp_path: Path):
    toml = tmp_path / "pyproject.toml"
    toml.write_text("""
[project]
dependencies = ["fastapi>=0.100", "celery>=5.0", "openai>=1.0"]
""")
    stack = detect_stack(tmp_path, SPEC)
    assert "web" in stack
    assert "fastapi" in stack["web"]
    assert "task_queue" in stack
    assert "llm" in stack


def test_detect_deps_deduplicates(tmp_path: Path):
    toml = tmp_path / "pyproject.toml"
    toml.write_text("""
[project]
dependencies = ["requests>=2.0"]
[project.optional-dependencies]
dev = ["requests>=2.0"]
""")
    deps = detect_deps(tmp_path)
    assert deps.count("requests") == 1


def test_detect_deps_requirements_txt(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask>=2.0\nrequests\n# comment\n-r other.txt\n")
    deps = detect_deps(tmp_path)
    assert "flask" in deps
    assert "requests" in deps
