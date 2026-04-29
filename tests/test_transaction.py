"""Tests for the transactional write surface (rollback + undo)."""

from pathlib import Path

import pytest

from wyolet.symbol.writes.transaction import FileEdit, commit_edits
from wyolet.symbol.writes.undo import undo_last


@pytest.fixture
def project(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    return tmp_path


def _edit(project: Path, name: str, new_content: str) -> FileEdit:
    return FileEdit(
        file_abs=project / name,
        file_rel=name,
        new_content=new_content.encode("utf-8"),
    )


def test_successful_commit_writes_all_files(project):
    edits = [_edit(project, "a.txt", "ALPHA\n"), _edit(project, "b.txt", "BETA\n")]
    result = commit_edits(edits, project_root=project, op_name="test", subject="x")
    assert result.status == "committed"
    assert (project / "a.txt").read_text() == "ALPHA\n"
    assert (project / "b.txt").read_text() == "BETA\n"
    assert result.transaction_id is not None


def test_persists_transaction_dir(project):
    edits = [_edit(project, "a.txt", "ALPHA\n")]
    result = commit_edits(edits, project_root=project, op_name="rename-symbol", subject="x")
    tx_dir = project / ".symbol" / "transactions" / result.transaction_id
    assert tx_dir.is_dir()
    assert (tx_dir / "manifest.json").exists()
    pre_blobs = list(tx_dir.glob("*.pre"))
    assert len(pre_blobs) == 1


def test_rollback_on_mid_write_failure(project, monkeypatch):
    """If write 2 of 2 fails, write 1 must be reverted to its pre-image."""
    from wyolet.symbol.writes import transaction as tx_mod

    real_atomic = tx_mod._atomic_write
    calls = {"n": 0}

    def flaky_write(path, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated mid-write failure")
        return real_atomic(path, data)

    monkeypatch.setattr(tx_mod, "_atomic_write", flaky_write)

    edits = [_edit(project, "a.txt", "ALPHA\n"), _edit(project, "b.txt", "BETA\n")]
    result = commit_edits(edits, project_root=project, op_name="test", subject="x")
    assert result.status == "error"
    assert result.error_code == "write_failed"
    # Rollback restored the original via real atomic_write (call 3+).
    assert (project / "a.txt").read_text() == "alpha\n"
    assert (project / "b.txt").read_text() == "beta\n"


def test_undo_restores_pre_images(project):
    edits = [_edit(project, "a.txt", "ALPHA\n"), _edit(project, "b.txt", "BETA\n")]
    commit_edits(edits, project_root=project, op_name="rename-symbol", subject="x")

    result = undo_last(project)
    assert result.status == "undone"
    assert (project / "a.txt").read_text() == "alpha\n"
    assert (project / "b.txt").read_text() == "beta\n"


def test_undo_creates_marker_so_next_undo_is_noop(project):
    edits = [_edit(project, "a.txt", "ALPHA\n")]
    commit_edits(edits, project_root=project, op_name="rename-symbol", subject="x")
    undo_last(project)
    second = undo_last(project)
    assert second.status == "nothing_to_undo"


def test_undo_on_empty_project(tmp_path):
    result = undo_last(tmp_path)
    assert result.status == "nothing_to_undo"


def test_undo_walks_back_through_history(project):
    """Two ops; undo reverts the most recent only."""
    import time

    commit_edits(
        [_edit(project, "a.txt", "FIRST\n")],
        project_root=project, op_name="rename-symbol", subject="op1",
    )
    time.sleep(0.01)  # ensure timestamp ordering
    commit_edits(
        [_edit(project, "a.txt", "SECOND\n")],
        project_root=project, op_name="rename-symbol", subject="op2",
    )

    undo_last(project)
    assert (project / "a.txt").read_text() == "FIRST\n"
    undo_last(project)
    assert (project / "a.txt").read_text() == "alpha\n"
