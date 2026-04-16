"""Tests for `ca rename-symbol` v1."""

import subprocess
from pathlib import Path

import pytest

from ca_tools.shared.symbol_index import SymbolIndex, get_or_build_index
from ca_tools.writes.rename_symbol import (
    RenameSymbolRequest,
    RenameSymbolResult,
    apply_rename_symbol,
    resolve_rename_symbol,
)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _index(project: Path) -> SymbolIndex:
    idx, _ = get_or_build_index(project)
    return idx


@pytest.fixture
def project(tmp_path):
    (tmp_path / "services.py").write_text(
        '''class UserService:
    def save(self):
        return 1


class OrderService:
    def save(self):
        return 2
'''
    )
    (tmp_path / "caller.py").write_text(
        '''from services import UserService


def use():
    svc = UserService()
    return svc.save()
'''
    )
    _git_init(tmp_path)
    return tmp_path


# ---------------------------------------------------------- resolve


def test_reject_dotted_new_name(project):
    idx = _index(project)
    r = resolve_rename_symbol(idx, "services.UserService", "foo.Bar", project)
    assert isinstance(r, RenameSymbolResult)
    assert r.error_code == "invalid_argument"


def test_reject_identical_new_name(project):
    idx = _index(project)
    r = resolve_rename_symbol(idx, "services.UserService", "UserService", project)
    assert isinstance(r, RenameSymbolResult)
    assert r.error_code == "invalid_argument"


def test_symbol_not_found(project):
    idx = _index(project)
    r = resolve_rename_symbol(idx, "services.Missing", "Foo", project)
    assert isinstance(r, RenameSymbolResult)
    assert r.error_code == "symbol_not_found"


def test_name_collision(project):
    idx = _index(project)
    r = resolve_rename_symbol(idx, "services.UserService", "OrderService", project)
    assert isinstance(r, RenameSymbolResult)
    assert r.error_code == "name_collision"


def test_resolve_collects_affected_files(project):
    idx = _index(project)
    r = resolve_rename_symbol(idx, "services.UserService", "NewUserService", project)
    assert isinstance(r, RenameSymbolRequest)
    files = {e.file_rel for e in r.edits}
    assert "services.py" in files
    assert "caller.py" in files


# ---------------------------------------------------------- apply


def test_apply_updates_declaration_and_callers(project):
    idx = _index(project)
    req = resolve_rename_symbol(idx, "services.UserService", "NewUserService", project)
    assert isinstance(req, RenameSymbolRequest)

    result = apply_rename_symbol(req, project_root=project)
    assert result.status == "applied"

    services = (project / "services.py").read_text()
    caller = (project / "caller.py").read_text()

    assert "class NewUserService" in services
    assert "class UserService" not in services
    # Sibling OrderService unchanged.
    assert "class OrderService" in services

    assert "from services import NewUserService" in caller
    assert "NewUserService()" in caller


def test_word_boundary_respects_substrings(tmp_path):
    """Renaming `save` shouldn't touch `saved_value` or similar substrings."""
    (tmp_path / "m.py").write_text(
        '''def save():
    return 1


def use():
    saved_value = 42  # substring — must not change
    return save()
'''
    )
    _git_init(tmp_path)

    idx = _index(tmp_path)
    req = resolve_rename_symbol(idx, "m.save", "persist", tmp_path)
    assert isinstance(req, RenameSymbolRequest)

    apply_rename_symbol(req, project_root=tmp_path)
    text = (tmp_path / "m.py").read_text()
    assert "def persist" in text
    assert "def save" not in text
    assert "saved_value = 42" in text   # substring preserved
    assert "return persist()" in text


def test_dry_run_does_not_write(project):
    idx = _index(project)
    before_services = (project / "services.py").read_text()
    before_caller = (project / "caller.py").read_text()

    req = resolve_rename_symbol(idx, "services.UserService", "NewUserService", project)
    assert isinstance(req, RenameSymbolRequest)

    result = apply_rename_symbol(req, project_root=project, dry_run=True)
    assert result.status == "dry_run"
    assert (project / "services.py").read_text() == before_services
    assert (project / "caller.py").read_text() == before_caller
    assert result.files_changed == 2
    assert result.refs_updated >= 3   # class def + import + call site at minimum


def test_per_file_counts_reported(project):
    idx = _index(project)
    req = resolve_rename_symbol(idx, "services.UserService", "NewUserService", project)
    assert isinstance(req, RenameSymbolRequest)
    result = apply_rename_symbol(req, project_root=project, dry_run=True)

    per_file = {f.file: f.refs_updated for f in result.per_file}
    assert "services.py" in per_file
    assert "caller.py" in per_file
    assert per_file["services.py"] >= 1
    assert per_file["caller.py"] >= 2   # import + constructor call


# ---------------------------------------------------------- git safety


def test_dirty_tree_refused(project):
    """Uncommitted changes to TRACKED files block rename unless --allow-dirty."""
    # Modify a tracked file (untracked files are ignored, by design).
    (project / "caller.py").write_text("# modified unexpectedly\n")

    idx = _index(project)
    req = resolve_rename_symbol(idx, "services.UserService", "NewUserService", project)
    assert isinstance(req, RenameSymbolRequest)

    result = apply_rename_symbol(req, project_root=project)
    assert result.status == "error"
    assert result.error_code == "working_tree_dirty"


def test_allow_dirty_proceeds(project):
    (project / "caller.py").write_text("# modified unexpectedly\n")

    idx = _index(project)
    req = resolve_rename_symbol(idx, "services.UserService", "NewUserService", project)
    assert isinstance(req, RenameSymbolRequest)

    result = apply_rename_symbol(req, project_root=project, allow_dirty=True)
    assert result.status == "applied"


def test_untracked_files_do_not_block_rename(project):
    """Untracked build artifacts (like .ca-tools/) don't count as dirty."""
    (project / "untracked_garbage.log").write_text("noise\n")

    idx = _index(project)
    req = resolve_rename_symbol(idx, "services.UserService", "NewUserService", project)
    assert isinstance(req, RenameSymbolRequest)

    result = apply_rename_symbol(req, project_root=project)
    assert result.status == "applied"


def test_non_git_project_refused(tmp_path):
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n")
    # no git init

    idx = _index(tmp_path)
    req = resolve_rename_symbol(idx, "m.foo", "bar", tmp_path)
    assert isinstance(req, RenameSymbolRequest)

    result = apply_rename_symbol(req, project_root=tmp_path)
    assert result.status == "error"
    assert result.error_code == "no_git_repository"


def test_non_git_with_force_no_vcs(tmp_path):
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n")

    idx = _index(tmp_path)
    req = resolve_rename_symbol(idx, "m.foo", "bar", tmp_path)
    assert isinstance(req, RenameSymbolRequest)

    result = apply_rename_symbol(req, project_root=tmp_path, force_no_vcs=True)
    assert result.status == "applied"
