"""Tests for orphan file detection."""

from pathlib import Path

from ca_tools.shared.import_graph import build_import_graph, detect_orphans


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    """Helper to create a project structure from a dict of path→content."""
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return tmp_path


def test_connected_files_not_orphans(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "main.py": "from lib import helper\nhelper()",
            "lib.py": "def helper(): pass",
        },
    )
    orphans = detect_orphans(tmp_path)
    orphan_names = [o.filepath.name for o in orphans]
    assert "lib.py" not in orphan_names
    # main.py is an orphan (nothing imports it)
    assert "main.py" in orphan_names


def test_disconnected_file_is_orphan(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "main.py": "print('hello')",
            "unused.py": "def unused(): pass",
        },
    )
    orphans = detect_orphans(tmp_path)
    orphan_names = [o.filepath.name for o in orphans]
    assert "unused.py" in orphan_names


def test_init_py_not_orphan(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "x = 1",
        },
    )
    orphans = detect_orphans(tmp_path)
    orphan_names = [o.filepath.name for o in orphans]
    assert "__init__.py" not in orphan_names


def test_test_files_not_orphans(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "test_foo.py": "def test_something(): pass",
            "conftest.py": "import pytest",
            "foo_test.py": "def test_other(): pass",
        },
    )
    orphans = detect_orphans(tmp_path)
    assert len(orphans) == 0


def test_script_classified_correctly(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "scripts/migrate.py": "print('migrating')",
        },
    )
    orphans = detect_orphans(tmp_path)
    assert len(orphans) == 1
    assert orphans[0].reason == "likely one-off script"


def test_import_graph_stats(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "a.py": "from b import x",
            "b.py": "from c import y\nx = 1",
            "c.py": "y = 2",
            "orphan.py": "z = 3",
        },
    )
    graph = build_import_graph(tmp_path)
    assert len(graph.files) == 4
    assert len(graph.resolved_edges.get(tmp_path / "a.py", set())) >= 1


def test_exclude_in_graph(tmp_path: Path):
    _make_project(
        tmp_path,
        {
            "src/app.py": "from src.lib import x",
            "src/lib.py": "x = 1",
            "scripts/run.py": "print('run')",
        },
    )
    graph = build_import_graph(tmp_path, exclude=["scripts/*"])
    file_names = [f.name for f in graph.files]
    assert "run.py" not in file_names
