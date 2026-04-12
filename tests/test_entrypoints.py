"""Tests for entry point detection."""

from pathlib import Path

from ca_tools.audit.entrypoints import detect_entrypoints


def test_detects_main_guard(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("""
def main():
    pass

if __name__ == "__main__":
    main()
""")
    eps = detect_entrypoints(tmp_path)
    assert len(eps) == 1
    assert eps[0].description == "main()"
    assert eps[0].in_main_guard is True


def test_detects_uvicorn_run(tmp_path: Path):
    f = tmp_path / "server.py"
    f.write_text("""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000)
""")
    eps = detect_entrypoints(tmp_path)
    assert len(eps) == 1
    assert "uvicorn.run" in eps[0].description


def test_detects_reverse_main_guard(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("""
if "__main__" == __name__:
    print("hello")
""")
    eps = detect_entrypoints(tmp_path)
    assert len(eps) == 1


def test_no_main_guard(tmp_path: Path):
    f = tmp_path / "lib.py"
    f.write_text("""
def helper():
    return 42
""")
    eps = detect_entrypoints(tmp_path)
    assert len(eps) == 0


def test_empty_main_guard(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("""
if __name__ == "__main__":
    pass
""")
    eps = detect_entrypoints(tmp_path)
    assert len(eps) == 1
    assert eps[0].description == "if __name__ == '__main__'"


def test_skips_venv(tmp_path: Path):
    venv = tmp_path / "venv" / "lib"
    venv.mkdir(parents=True)
    f = venv / "script.py"
    f.write_text('if __name__ == "__main__": pass')
    eps = detect_entrypoints(tmp_path)
    assert len(eps) == 0


def test_exclude_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "app.py").write_text('if __name__ == "__main__": main()')
    (tmp_path / "scripts" / "run.py").write_text('if __name__ == "__main__": run()')
    eps = detect_entrypoints(tmp_path, exclude=["scripts/*"])
    assert len(eps) == 1
    assert "app.py" in str(eps[0].filepath)


def test_include_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "app.py").write_text('if __name__ == "__main__": main()')
    (tmp_path / "scripts" / "run.py").write_text('if __name__ == "__main__": run()')
    eps = detect_entrypoints(tmp_path, include=["src/*"])
    assert len(eps) == 1
    assert "app.py" in str(eps[0].filepath)
