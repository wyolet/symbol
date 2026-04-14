"""Tests for entry point detection."""

from pathlib import Path

import ca_tools.checkers  # noqa: F401
from ca_tools.checkers.entrypoints import detect
from ca_tools.shared.context import build_context


def _run(tmp_path: Path, **kwargs):
    ctx = build_context(tmp_path, **kwargs)
    results = []
    for filepath in ctx.cache.files:
        tree = ctx.cache.get_ast(filepath)
        results.extend(detect(ctx, filepath, tree))
    return results


def test_detects_main_guard(tmp_path: Path):
    (tmp_path / "app.py").write_text("""
def main():
    pass

if __name__ == "__main__":
    main()
""")
    eps = _run(tmp_path)
    assert len(eps) == 1
    assert eps[0].description == "main()"
    assert eps[0].in_main_guard is True


def test_detects_uvicorn_run(tmp_path: Path):
    (tmp_path / "server.py").write_text("""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000)
""")
    eps = _run(tmp_path)
    assert len(eps) == 1
    assert "uvicorn.run" in eps[0].description


def test_detects_reverse_main_guard(tmp_path: Path):
    (tmp_path / "app.py").write_text("""
if "__main__" == __name__:
    print("hello")
""")
    eps = _run(tmp_path)
    assert len(eps) == 1


def test_no_main_guard(tmp_path: Path):
    (tmp_path / "lib.py").write_text("""
def helper():
    return 42
""")
    eps = _run(tmp_path)
    assert len(eps) == 0


def test_empty_main_guard(tmp_path: Path):
    (tmp_path / "app.py").write_text("""
if __name__ == "__main__":
    pass
""")
    eps = _run(tmp_path)
    assert len(eps) == 1
    assert eps[0].description == "if __name__ == '__main__'"


def test_skips_venv(tmp_path: Path):
    venv = tmp_path / "venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "script.py").write_text('if __name__ == "__main__": pass')
    eps = _run(tmp_path)
    assert len(eps) == 0


def test_exclude_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "app.py").write_text('if __name__ == "__main__": main()')
    (tmp_path / "scripts" / "run.py").write_text('if __name__ == "__main__": run()')
    eps = _run(tmp_path, exclude=["scripts/*"])
    assert len(eps) == 1
    assert "app.py" in str(eps[0].filepath)


def test_include_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "app.py").write_text('if __name__ == "__main__": main()')
    (tmp_path / "scripts" / "run.py").write_text('if __name__ == "__main__": run()')
    eps = _run(tmp_path, include=["src/*"])
    assert len(eps) == 1
    assert "app.py" in str(eps[0].filepath)
