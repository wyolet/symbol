"""Tests for `ca patch` stage 1: argument validation + cache preflight."""

import hashlib
import os
import time
from pathlib import Path

import pytest

from ca.symbol.caches import InMemoryReadCache
from ca.symbol.protocols import CachedRead
from ca.symbol.writes.patch import (
    InvalidRange,
    PatchPreflight,
    PatchRequest,
    line_range_to_byte_range,
    parse_line_range,
    preflight_patch,
    validate_args,
)


# ---------------------------------------------------------- parse_line_range


def test_parse_line_range_basic():
    assert parse_line_range("10-20") == (10, 20)


def test_parse_line_range_same_line():
    assert parse_line_range("5-5") == (5, 5)


def test_parse_line_range_strips_whitespace():
    assert parse_line_range("  10-20  ") == (10, 20)


def test_parse_line_range_bad_format_raises():
    with pytest.raises(InvalidRange):
        parse_line_range("abc-def")


def test_parse_line_range_missing_dash_raises():
    with pytest.raises(InvalidRange):
        parse_line_range("10")


def test_parse_line_range_end_before_start_raises():
    with pytest.raises(InvalidRange):
        parse_line_range("20-10")


def test_parse_line_range_zero_start_raises():
    with pytest.raises(InvalidRange):
        parse_line_range("0-10")


# ---------------------------------------------------------- line_range_to_byte_range


def test_line_range_to_byte_range_first_line():
    data = b"first\nsecond\nthird\n"
    assert line_range_to_byte_range(data, (1, 1)) == (0, 6)


def test_line_range_to_byte_range_middle_line():
    data = b"first\nsecond\nthird\n"
    assert line_range_to_byte_range(data, (2, 2)) == (6, 13)


def test_line_range_to_byte_range_multi_line():
    data = b"a\nb\nc\nd\n"
    # lines 2-3 = "b\nc\n" = bytes 2..6
    assert line_range_to_byte_range(data, (2, 3)) == (2, 6)


def test_line_range_to_byte_range_last_line_no_newline():
    data = b"a\nb\nc"  # no trailing \n
    assert line_range_to_byte_range(data, (3, 3)) == (4, 5)


# ---------------------------------------------------------- validate_args


@pytest.fixture
def project(tmp_path):
    """A tiny project tree for arg validation tests."""
    (tmp_path / "src").mkdir()
    foo = tmp_path / "src" / "foo.py"
    foo.write_text("line one\nline two\nline three\nline four\n")
    return tmp_path


def test_validate_args_ok(project):
    result = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range="2-3",
        content="new content\n",
        project_root=project,
    )
    assert isinstance(result, PatchRequest)
    assert result.line_range == (2, 3)
    # lines 2-3 = "line two\nline three\n" = bytes 9..29
    assert result.byte_range == (9, 29)
    assert result.content == b"new content\n"
    assert result.file_rel == "src/foo.py"
    assert result.force is False


def test_validate_args_empty_content(project):
    result = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range="2-2",
        content="",
        project_root=project,
    )
    assert isinstance(result, PatchRequest)
    assert result.content == b""


def test_validate_args_none_content_is_empty(project):
    """None content (delete op) is equivalent to empty."""
    result = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range="2-2",
        content=None,
        project_root=project,
    )
    assert isinstance(result, PatchRequest)
    assert result.content == b""


def test_validate_args_bytes_content(project):
    result = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range="2-2",
        content=b"raw bytes\n",
        project_root=project,
    )
    assert isinstance(result, PatchRequest)
    assert result.content == b"raw bytes\n"


def test_validate_args_file_not_found(project):
    result = validate_args(
        file=str(project / "src" / "nope.py"),
        raw_range="1-1",
        content="",
        project_root=project,
    )
    assert isinstance(result, PatchPreflight)
    assert result.status == "error"
    assert result.error_code == "file_not_found"


def test_validate_args_file_is_directory(project):
    result = validate_args(
        file=str(project / "src"),
        raw_range="1-1",
        content="",
        project_root=project,
    )
    assert isinstance(result, PatchPreflight)
    assert result.error_code == "file_not_found"


def test_validate_args_bad_range(project):
    result = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range="abc",
        content="",
        project_root=project,
    )
    assert isinstance(result, PatchPreflight)
    assert result.error_code == "invalid_argument"


def test_validate_args_range_out_of_bounds(project):
    result = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range="1-100",
        content="",
        project_root=project,
    )
    assert isinstance(result, PatchPreflight)
    assert result.error_code == "range_out_of_bounds"


def test_validate_args_binary_file(project):
    binary = project / "binary.dat"
    binary.write_bytes(b"\x89PNG\x00\x00\x00garbage")
    result = validate_args(
        file=str(binary),
        raw_range="1-1",
        content="x",
        project_root=project,
    )
    assert isinstance(result, PatchPreflight)
    assert result.error_code == "binary_file"


def test_validate_args_force_flag_propagates(project):
    result = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range="2-2",
        content="x",
        project_root=project,
        force=True,
    )
    assert isinstance(result, PatchRequest)
    assert result.force is True


# ---------------------------------------------------------- preflight_patch


def _served_entry(file_abs: Path, file_rel: str, byte_range: tuple[int, int]) -> CachedRead:
    data = file_abs.read_bytes()
    served = data[byte_range[0] : byte_range[1]]
    return CachedRead(
        file=file_rel,
        byte_range=byte_range,
        content_hash=hashlib.sha256(served).hexdigest()[:16],
        served_at=time.time(),
        served_mtime=os.stat(file_abs).st_mtime,
        tool_call_idx=0,
    )


def _valid_request(project: Path, line_range="2-3") -> PatchRequest:
    req = validate_args(
        file=str(project / "src" / "foo.py"),
        raw_range=line_range,
        content="new\n",
        project_root=project,
    )
    assert isinstance(req, PatchRequest)
    return req


def test_preflight_ok_when_covering_entry_matches(project):
    req = _valid_request(project)
    cache = InMemoryReadCache()
    # Agent served wider range including the patch range.
    cache.record(_served_entry(req.file_abs, req.file_rel, (0, 40)))

    result = preflight_patch(req, cache)
    assert result.status == "ok"
    assert result.request is req
    assert result.cache_entry is not None


def test_preflight_ok_when_exact_range_served(project):
    req = _valid_request(project)
    cache = InMemoryReadCache()
    cache.record(_served_entry(req.file_abs, req.file_rel, req.byte_range))

    result = preflight_patch(req, cache)
    assert result.status == "ok"


def test_preflight_needs_confirmation_when_empty_cache(project):
    req = _valid_request(project)
    cache = InMemoryReadCache()

    result = preflight_patch(req, cache)
    assert result.status == "needs_read_confirmation"
    assert result.request is req
    assert result.current_byte_range == req.byte_range


def test_preflight_needs_confirmation_when_cache_partial(project):
    """Cached range doesn't cover the requested patch range."""
    req = _valid_request(project, line_range="2-3")  # bytes 9..29
    cache = InMemoryReadCache()
    # Agent saw lines 1-1 only — doesn't cover lines 2-3.
    cache.record(_served_entry(req.file_abs, req.file_rel, (0, 8)))

    result = preflight_patch(req, cache)
    assert result.status == "needs_read_confirmation"


def test_preflight_needs_confirmation_when_cache_offset(project):
    """Cache covers a later region but not the requested range."""
    req = _valid_request(project, line_range="1-1")  # bytes 0..9
    cache = InMemoryReadCache()
    cache.record(_served_entry(req.file_abs, req.file_rel, (9, 29)))

    result = preflight_patch(req, cache)
    assert result.status == "needs_read_confirmation"


def test_preflight_force_bypasses_cache(project):
    req_no_force = _valid_request(project)
    # Rebuild with force=True
    req = PatchRequest(
        file_abs=req_no_force.file_abs,
        file_rel=req_no_force.file_rel,
        line_range=req_no_force.line_range,
        byte_range=req_no_force.byte_range,
        content=req_no_force.content,
        force=True,
    )
    cache = InMemoryReadCache()  # intentionally empty

    result = preflight_patch(req, cache)
    assert result.status == "ok"
    assert result.cache_entry is None  # force doesn't attach an entry


def test_preflight_mtime_mismatch_treated_as_confirmation(project):
    """Stage 1: if mtime changed, we fall back to confirmation (stage 2 adds rehash)."""
    req = _valid_request(project)
    cache = InMemoryReadCache()
    # Manually construct an entry with a bogus old mtime.
    data = req.file_abs.read_bytes()
    served = data[req.byte_range[0] : req.byte_range[1]]
    entry = CachedRead(
        file=req.file_rel,
        byte_range=(0, 40),
        content_hash=hashlib.sha256(served).hexdigest()[:16],
        served_at=time.time(),
        served_mtime=1.0,  # clearly old
        tool_call_idx=0,
    )
    cache.record(entry)

    result = preflight_patch(req, cache)
    assert result.status == "needs_read_confirmation"


def test_preflight_picks_most_recent_covering_entry(project):
    req = _valid_request(project)
    cache = InMemoryReadCache()
    # Two covering entries; newer should win even if older has same mtime.
    older = _served_entry(req.file_abs, req.file_rel, (0, 40))
    newer = CachedRead(
        file=older.file,
        byte_range=(0, 50),
        content_hash=older.content_hash,
        served_at=older.served_at + 10,
        served_mtime=older.served_mtime,
        tool_call_idx=1,
    )
    cache.record(older)
    cache.record(newer)

    result = preflight_patch(req, cache)
    assert result.status == "ok"
    assert result.cache_entry is not None
    assert result.cache_entry.byte_range == (0, 50)
