"""Tests for stack detection."""

from pathlib import Path

from ca.symbol.shared.deps import _extract_package_name, detect_deps, detect_stack
from ca.symbol.shared.spec import load_spec

SPEC = load_spec()


def test_extract_package_name():
    assert _extract_package_name("requests>=2.0") == "requests"
    assert _extract_package_name("flask[async]>=2.0") == "flask"
    assert _extract_package_name("django~=4.0") == "django"
    assert _extract_package_name("  numpy  ") == "numpy"
    assert _extract_package_name("") is None


def test_pep621_deps(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[project]
dependencies = [
    "fastapi>=0.100",
    "sqlalchemy[asyncio]>=2.0",
    "redis>=5.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]
""")
    deps = detect_deps(tmp_path)
    assert "fastapi" in deps
    assert "sqlalchemy" in deps
    assert "redis" in deps
    assert "pytest" in deps


def test_pep735_dependency_groups(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[project]
dependencies = ["sqlmodel>=0.0.27"]

[dependency-groups]
api = ["fastapi>=0.119", "uvicorn>=0.37"]
dev = ["pytest>=8.0", "ruff>=0.5"]
""")
    deps = detect_deps(tmp_path)
    assert "sqlmodel" in deps
    assert "fastapi" in deps
    assert "uvicorn" in deps
    assert "pytest" in deps


def test_poetry_deps(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[tool.poetry.dependencies]
python = "^3.11"
django = "^4.2"
celery = "^5.3"

[tool.poetry.group.dev.dependencies]
pytest = "^8.0"
""")
    deps = detect_deps(tmp_path)
    assert "django" in deps
    assert "celery" in deps
    assert "pytest" in deps
    assert "python" not in deps


def test_detect_stack(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
[project]
dependencies = ["fastapi>=0.100", "celery>=5.0", "openai>=1.0"]
""")
    stack = detect_stack(tmp_path, SPEC)
    assert "web" in stack
    assert "fastapi" in stack["web"]
    assert "task_queue" in stack
    assert "llm" in stack


def test_detect_deps_deduplicates(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("""
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


def test_detect_deps_pipfile(tmp_path: Path):
    (tmp_path / "Pipfile").write_text("""
[packages]
django = ">=4.0"
requests = "*"

[dev-packages]
pytest = "*"
""")
    deps = detect_deps(tmp_path)
    assert "django" in deps
    assert "requests" in deps
