"""Tests for the registered orphans checker."""

from pathlib import Path

import pytest

from ca_tools.checkers.orphans import OrphanFile, detect as detect_orphans_checker
from ca_tools.shared.context import build_context


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    """Helper to create a project structure from a dict of path→content."""
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def test_connected_files_not_orphans(tmp_path: Path):
    """main.py imports lib.py — lib.py should not be an orphan, main.py should be."""
    _make_project(
        tmp_path,
        {
            "main.py": "from lib import helper\nhelper()",
            "lib.py": "def helper(): pass",
        },
    )
    ctx = build_context(tmp_path)
    orphans = detect_orphans_checker(ctx)
    orphan_names = [o.filepath.name for o in orphans]
    assert "lib.py" not in orphan_names
    assert "main.py" in orphan_names


def test_init_py_not_orphan(tmp_path: Path):
    """__init__.py is always skipped — it is a package marker, not dead code."""
    _make_project(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "x = 1",
        },
    )
    ctx = build_context(tmp_path)
    orphans = detect_orphans_checker(ctx)
    orphan_names = [o.filepath.name for o in orphans]
    assert "__init__.py" not in orphan_names


def test_test_files_skipped(tmp_path: Path):
    """test_foo.py and foo_test.py match default skip patterns and are never orphans."""
    _make_project(
        tmp_path,
        {
            "test_foo.py": "def test_something(): pass",
            "foo_test.py": "def test_other(): pass",
        },
    )
    ctx = build_context(tmp_path)
    orphans = detect_orphans_checker(ctx)
    orphan_names = [o.filepath.name for o in orphans]
    assert "test_foo.py" not in orphan_names
    assert "foo_test.py" not in orphan_names


def test_main_guard_not_orphan(tmp_path: Path):
    """A file with `if __name__ == '__main__':` is treated as an entry point."""
    _make_project(
        tmp_path,
        {
            "runner.py": (
                "def run():\n"
                "    print('running')\n\n"
                "if __name__ == '__main__':\n"
                "    run()\n"
            ),
        },
    )
    ctx = build_context(tmp_path)
    orphans = detect_orphans_checker(ctx)
    orphan_names = [o.filepath.name for o in orphans]
    assert "runner.py" not in orphan_names


def test_skip_pattern_respected(tmp_path: Path):
    """Files matching skip_orphan_patterns from pyproject.toml are excluded."""
    _make_project(
        tmp_path,
        {
            "scripts/deploy.py": "print('deploying')",
            "app.py": "x = 1",
            "pyproject.toml": (
                "[tool.ca-tools.checkers.orphans]\n"
                'ignore = ["scripts/*"]\n'
            ),
        },
    )
    ctx = build_context(tmp_path)
    orphans = detect_orphans_checker(ctx)
    orphan_paths = [str(o.filepath.relative_to(tmp_path)) for o in orphans]
    assert not any("scripts" in p for p in orphan_paths), (
        f"scripts/ files should be skipped but got: {orphan_paths}"
    )


def test_dead_code_classified(tmp_path: Path):
    """An unreachable file with no special path gets reason='likely dead code'."""
    _make_project(
        tmp_path,
        {
            "utils.py": "def helper(): pass",
        },
    )
    ctx = build_context(tmp_path)
    orphans = detect_orphans_checker(ctx)
    dead = [o for o in orphans if o.filepath.name == "utils.py"]
    assert dead, "utils.py should be detected as an orphan"
    assert dead[0].reason == "likely dead code"
