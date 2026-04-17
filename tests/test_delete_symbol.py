"""Tests for `ca delete-symbol` v1."""

from pathlib import Path

import pytest

from ca.symbol.caches import NullReadCache
from ca.symbol.shared.symbol_index import SymbolIndex, get_or_build_index
from ca.symbol.writes.delete_symbol import (
    DeleteSymbolRequest,
    DeleteSymbolResult,
    apply_delete_symbol,
    resolve_delete_symbol,
)


def _build_index(project_root: Path) -> SymbolIndex:
    idx, _ = get_or_build_index(project_root)
    return idx


@pytest.fixture
def project(tmp_path):
    (tmp_path / "services.py").write_text(
        '''"""Services module."""


class UserService:
    def save(self, user):
        return user


class OrderService:
    def place(self, order):
        return order


def free_function():
    return 42
'''
    )
    (tmp_path / "caller.py").write_text(
        '''from services import UserService


def use_it():
    svc = UserService()
    return svc.save(None)
'''
    )
    return tmp_path


# ---------------------------------------------------------- resolve


def test_resolve_finds_module_function(project):
    idx = _build_index(project)
    result = resolve_delete_symbol(idx, "services.free_function", project)
    assert isinstance(result, DeleteSymbolRequest)
    assert result.qualified_path == "services.free_function"
    assert result.kind == "function"
    assert result.file_rel == "services.py"


def test_resolve_symbol_not_found(project):
    idx = _build_index(project)
    result = resolve_delete_symbol(idx, "services.does_not_exist", project)
    assert isinstance(result, DeleteSymbolResult)
    assert result.status == "error"
    assert result.error_code == "symbol_not_found"


def test_resolve_reports_callers_and_refuses(project):
    """UserService is referenced in caller.py; deletion should refuse."""
    idx = _build_index(project)
    result = resolve_delete_symbol(idx, "services.UserService", project)
    assert isinstance(result, DeleteSymbolResult)
    assert result.status == "error"
    assert result.error_code == "has_live_references"
    assert len(result.callers) >= 1
    caller_files = {c.file for c in result.callers}
    assert "caller.py" in caller_files


def test_resolve_force_bypasses_caller_check(project):
    idx = _build_index(project)
    result = resolve_delete_symbol(idx, "services.UserService", project, force=True)
    assert isinstance(result, DeleteSymbolRequest)
    # Callers are still reported on the request so the UI can warn.
    assert len(result.callers) >= 1


def test_resolve_no_callers_passes(project):
    """free_function is never called; deletion should proceed."""
    idx = _build_index(project)
    result = resolve_delete_symbol(idx, "services.free_function", project)
    assert isinstance(result, DeleteSymbolRequest)
    assert result.callers == ()


# ---------------------------------------------------------- apply


def test_apply_removes_symbol_from_file(project):
    idx = _build_index(project)
    req = resolve_delete_symbol(idx, "services.free_function", project)
    assert isinstance(req, DeleteSymbolRequest)

    result = apply_delete_symbol(req, cache=NullReadCache())

    assert result.status == "applied"
    content = (project / "services.py").read_text()
    assert "def free_function" not in content
    # Other symbols survive.
    assert "class UserService" in content
    assert "class OrderService" in content


def test_apply_dry_run_does_not_write(project):
    idx = _build_index(project)
    req = resolve_delete_symbol(idx, "services.free_function", project)
    assert isinstance(req, DeleteSymbolRequest)
    before = (project / "services.py").read_text()

    result = apply_delete_symbol(req, cache=NullReadCache(), dry_run=True)

    assert result.status == "dry_run"
    assert (project / "services.py").read_text() == before
    assert result.diff
    assert "def free_function" in result.diff


def test_apply_force_removes_symbol_with_callers(project):
    """--force deletes even though callers exist. Callers remain (broken)."""
    idx = _build_index(project)
    req = resolve_delete_symbol(idx, "services.UserService", project, force=True)
    assert isinstance(req, DeleteSymbolRequest)

    result = apply_delete_symbol(req, cache=NullReadCache())
    assert result.status == "applied"
    assert "class UserService" not in (project / "services.py").read_text()
    # caller.py is untouched.
    assert "UserService" in (project / "caller.py").read_text()


def test_apply_preserves_other_symbols(project):
    idx = _build_index(project)
    req = resolve_delete_symbol(idx, "services.OrderService", project)
    assert isinstance(req, DeleteSymbolRequest)

    result = apply_delete_symbol(req, cache=NullReadCache())
    assert result.status == "applied"

    content = (project / "services.py").read_text()
    assert "class OrderService" not in content
    # UserService still exists (its body is `save`, not affected).
    assert "class UserService" in content
    assert "def free_function" in content


# ---------------------------------------------------------- response payload


def test_result_carries_location_and_kind(project):
    idx = _build_index(project)
    req = resolve_delete_symbol(idx, "services.free_function", project)
    assert isinstance(req, DeleteSymbolRequest)
    result = apply_delete_symbol(req, cache=NullReadCache(), dry_run=True)

    assert result.qualified_path == "services.free_function"
    assert result.kind == "function"
    assert result.file_rel == "services.py"
    assert result.line_range[0] <= result.line_range[1]
    assert result.lines_removed > 0
