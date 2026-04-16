"""Tests for `ca patch` stage 2: apply."""

import hashlib
import os
import stat
import time
from pathlib import Path

import pytest

from ca_tools.caches import InMemoryReadCache
from ca_tools.protocols import CachedRead
from ca_tools.writes.patch import (
    PatchRequest,
    PatchResult,
    apply_patch,
    validate_args,
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    foo = tmp_path / "src" / "foo.py"
    foo.write_text("line one\nline two\nline three\nline four\n")
    return tmp_path


def _request(project, line_range: str, content: str | None = "new\n") -> PatchRequest:
    req = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range=line_range,
        content=content,
        project_root=project,
    )
    assert isinstance(req, PatchRequest)
    return req


def _cache_covering(req: PatchRequest) -> InMemoryReadCache:
    cache = InMemoryReadCache()
    data = req.file_abs.read_bytes()
    entry = CachedRead(
        file=req.file_rel,
        byte_range=(0, len(data)),
        content_hash=hashlib.sha256(data).hexdigest()[:16],
        served_at=time.time(),
        served_mtime=os.stat(req.file_abs).st_mtime,
        tool_call_idx=0,
    )
    cache.record(entry)
    return cache


# ---------------------------------------------------------- replace


def test_apply_replace_line(project):
    req = _request(project, "2-2", content="replaced\n")
    cache = _cache_covering(req)

    result = apply_patch(req, cache=cache)

    assert result.status == "applied"
    assert result.file_rel == "src/foo.py"
    assert req.file_abs.read_text() == "line one\nreplaced\nline three\nline four\n"
    assert result.lines_removed == 1
    assert result.lines_added == 1


def test_apply_replace_multi_line(project):
    req = _request(project, "2-3", content="a\nb\nc\n")
    cache = _cache_covering(req)

    result = apply_patch(req, cache=cache)

    assert result.status == "applied"
    assert req.file_abs.read_text() == "line one\na\nb\nc\nline four\n"
    assert result.lines_removed == 2
    assert result.lines_added == 3


# ---------------------------------------------------------- delete


def test_apply_delete_is_empty_content(project):
    req = _request(project, "2-2", content="")
    cache = _cache_covering(req)

    result = apply_patch(req, cache=cache)

    assert result.status == "applied"
    assert req.file_abs.read_text() == "line one\nline three\nline four\n"
    assert result.lines_removed == 1
    assert result.lines_added == 0


def test_apply_delete_with_none_content(project):
    req = _request(project, "3-3", content=None)
    cache = _cache_covering(req)

    result = apply_patch(req, cache=cache)

    assert result.status == "applied"
    assert req.file_abs.read_text() == "line one\nline two\nline four\n"


# ---------------------------------------------------------- diff


def test_apply_returns_unified_diff(project):
    req = _request(project, "2-2", content="replaced\n")
    cache = _cache_covering(req)

    result = apply_patch(req, cache=cache)

    assert "line two" in result.diff
    assert "replaced" in result.diff
    assert result.diff.startswith("---")


# ---------------------------------------------------------- cache invalidation


def test_apply_invalidates_cache(project):
    req = _request(project, "2-2", content="x\n")
    cache = _cache_covering(req)

    # Sanity: cache has an entry for the file.
    assert cache.find_covering(Path(req.file_rel), req.byte_range) is not None

    apply_patch(req, cache=cache)

    # Entry gone after apply.
    assert cache.find_covering(Path(req.file_rel), req.byte_range) is None


# ---------------------------------------------------------- dry run


def test_dry_run_does_not_write(project):
    req = _request(project, "2-2", content="replaced\n")
    cache = _cache_covering(req)
    original = req.file_abs.read_text()

    result = apply_patch(req, cache=cache, dry_run=True)

    assert result.status == "dry_run"
    assert req.file_abs.read_text() == original  # unchanged
    # cache still has the pre-patch entry
    assert cache.find_covering(Path(req.file_rel), req.byte_range) is not None
    # diff still computed
    assert "replaced" in result.diff


# ---------------------------------------------------------- atomicity


def test_apply_preserves_file_mode(project):
    """File permission bits survive the atomic rename."""
    foo = project / "src" / "foo.py"
    os.chmod(foo, 0o644)

    req = _request(project, "2-2", content="x\n")
    cache = _cache_covering(req)

    apply_patch(req, cache=cache)

    mode = stat.S_IMODE(os.stat(foo).st_mode)
    assert mode == 0o644


def test_apply_cleans_up_tmp_on_success(project):
    """After a successful write, no .ca-tools.tmp-* files linger."""
    req = _request(project, "2-2", content="x\n")
    cache = _cache_covering(req)

    apply_patch(req, cache=cache)

    for p in (project / "src").iterdir():
        assert not p.name.startswith(".ca-tools.tmp-")


# ---------------------------------------------------------- race / conflict


def test_apply_conflict_when_file_shrinks_between_preflight_and_apply(project):
    req = _request(project, "3-4", content="x\n")
    cache = _cache_covering(req)

    # Simulate the file being truncated by someone else.
    req.file_abs.write_text("only one line\n")

    result = apply_patch(req, cache=cache)

    assert result.status == "error"
    assert result.error_code == "conflict"


# ---------------------------------------------------------- error paths


def test_apply_missing_file_returns_error(project):
    req = _request(project, "2-2", content="x\n")
    cache = _cache_covering(req)

    req.file_abs.unlink()

    result = apply_patch(req, cache=cache)
    assert result.status == "error"
    assert result.error_code == "file_not_found"


def test_apply_permission_denied_returns_error(project):
    req = _request(project, "2-2", content="x\n")
    cache = _cache_covering(req)

    # Make the parent dir read-only so the tmp file can't be created.
    os.chmod(project / "src", 0o555)
    try:
        result = apply_patch(req, cache=cache)
        assert result.status == "error"
        assert result.error_code == "permission_denied"
    finally:
        os.chmod(project / "src", 0o755)


# ---------------------------------------------------------- zero-width insert


def test_apply_zero_width_insert_pattern_via_replace(project):
    """Inserting is 'replace empty range with content' — smallest range is 1 line."""
    req = _request(project, "1-1", content="prepended\nline one\n")
    cache = _cache_covering(req)

    result = apply_patch(req, cache=cache)
    assert result.status == "applied"
    assert req.file_abs.read_text() == "prepended\nline one\nline two\nline three\nline four\n"
