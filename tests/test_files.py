"""Tests for shared file collection."""

from pathlib import Path

from ca_tools.shared.files import collect_py_files


def _make_files(tmp_path: Path, paths: list[str]) -> None:
    for p in paths:
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# placeholder")


def test_collects_py_files(tmp_path: Path):
    _make_files(tmp_path, ["a.py", "b.py", "c.txt"])
    files = collect_py_files(tmp_path)
    names = [f.name for f in files]
    assert "a.py" in names
    assert "b.py" in names
    assert "c.txt" not in names


def test_skips_hidden_dirs(tmp_path: Path):
    _make_files(tmp_path, [".hidden/mod.py", "visible.py"])
    files = collect_py_files(tmp_path)
    names = [f.name for f in files]
    assert "visible.py" in names
    assert "mod.py" not in names


def test_skips_venv(tmp_path: Path):
    _make_files(tmp_path, ["venv/lib/pkg.py", ".venv/lib/pkg.py", "app.py"])
    files = collect_py_files(tmp_path)
    assert len(files) == 1
    assert files[0].name == "app.py"


def test_skips_pycache(tmp_path: Path):
    _make_files(tmp_path, ["__pycache__/mod.cpython-313.py", "app.py"])
    files = collect_py_files(tmp_path)
    assert len(files) == 1


def test_include_filter(tmp_path: Path):
    _make_files(tmp_path, ["src/app.py", "tests/test_app.py", "scripts/run.py"])
    files = collect_py_files(tmp_path, include=["src/*"])
    names = [f.name for f in files]
    assert "app.py" in names
    assert "test_app.py" not in names
    assert "run.py" not in names


def test_exclude_filter(tmp_path: Path):
    _make_files(tmp_path, ["src/app.py", "tests/test_app.py", "scripts/run.py"])
    files = collect_py_files(tmp_path, exclude=["tests/*", "scripts/*"])
    names = [f.name for f in files]
    assert "app.py" in names
    assert "test_app.py" not in names
    assert "run.py" not in names


def test_include_and_exclude(tmp_path: Path):
    _make_files(tmp_path, ["src/app.py", "src/vendor/lib.py", "tests/test_app.py"])
    files = collect_py_files(tmp_path, include=["src/*.py", "src/**/*.py"], exclude=["src/vendor/*"])
    names = [f.name for f in files]
    assert "app.py" in names
    assert "lib.py" not in names
    assert "test_app.py" not in names
