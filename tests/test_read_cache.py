"""Tests for read cache implementations and selection."""

import os
import time
from pathlib import Path

import pytest

from wyolet.symbol.caches import (
    DiskReadCache,
    InMemoryReadCache,
    NullReadCache,
    build_read_cache,
)
from wyolet.symbol.protocols import CachedRead, ReadCache


def _entry(file="src/foo.py", start=10, end=50, h="abc"):
    return CachedRead(
        file=file,
        byte_range=(start, end),
        content_hash=h,
        served_at=time.time(),
        served_mtime=1000.0,
        tool_call_idx=1,
    )


# ---------------------------------------------------------- protocol conformance


@pytest.fixture
def in_mem() -> InMemoryReadCache:
    return InMemoryReadCache()


@pytest.fixture
def disk(tmp_path, monkeypatch) -> DiskReadCache:
    monkeypatch.chdir(tmp_path)
    return DiskReadCache(session_id="test-session", project_root=tmp_path)


@pytest.fixture
def null() -> NullReadCache:
    return NullReadCache()


def test_in_memory_satisfies_protocol(in_mem):
    assert isinstance(in_mem, ReadCache)


def test_disk_satisfies_protocol(disk):
    assert isinstance(disk, ReadCache)


def test_null_satisfies_protocol(null):
    assert isinstance(null, ReadCache)


# ---------------------------------------------------------- record + lookup


@pytest.mark.parametrize("cache_fixture", ["in_mem", "disk"])
def test_record_then_lookup(cache_fixture, request):
    cache = request.getfixturevalue(cache_fixture)
    e = _entry()
    cache.record(e)

    got = cache.lookup(Path("src/foo.py"), (10, 50))
    assert got is not None
    assert got.content_hash == "abc"
    assert got.byte_range == (10, 50)


@pytest.mark.parametrize("cache_fixture", ["in_mem", "disk"])
def test_lookup_miss_returns_none(cache_fixture, request):
    cache = request.getfixturevalue(cache_fixture)
    assert cache.lookup(Path("src/nope.py"), (0, 10)) is None


@pytest.mark.parametrize("cache_fixture", ["in_mem", "disk"])
def test_record_overwrites_same_range(cache_fixture, request):
    cache = request.getfixturevalue(cache_fixture)
    cache.record(_entry(h="first"))
    cache.record(_entry(h="second"))
    got = cache.lookup(Path("src/foo.py"), (10, 50))
    assert got is not None
    assert got.content_hash == "second"


@pytest.mark.parametrize("cache_fixture", ["in_mem", "disk"])
def test_lookup_is_exact_range(cache_fixture, request):
    """A range lookup matches only the exact range recorded, not overlaps."""
    cache = request.getfixturevalue(cache_fixture)
    cache.record(_entry(start=10, end=50))
    assert cache.lookup(Path("src/foo.py"), (10, 50)) is not None
    assert cache.lookup(Path("src/foo.py"), (10, 49)) is None
    assert cache.lookup(Path("src/foo.py"), (11, 50)) is None


# ---------------------------------------------------------- invalidate


@pytest.mark.parametrize("cache_fixture", ["in_mem", "disk"])
def test_invalidate_drops_all_entries_for_file(cache_fixture, request):
    cache = request.getfixturevalue(cache_fixture)
    cache.record(_entry(file="src/foo.py", start=0, end=10))
    cache.record(_entry(file="src/foo.py", start=50, end=80))
    cache.record(_entry(file="src/bar.py", start=0, end=10))

    cache.invalidate(Path("src/foo.py"))

    assert cache.lookup(Path("src/foo.py"), (0, 10)) is None
    assert cache.lookup(Path("src/foo.py"), (50, 80)) is None
    # Other files untouched.
    assert cache.lookup(Path("src/bar.py"), (0, 10)) is not None


@pytest.mark.parametrize("cache_fixture", ["in_mem", "disk"])
def test_invalidate_missing_file_is_noop(cache_fixture, request):
    cache = request.getfixturevalue(cache_fixture)
    cache.invalidate(Path("never-recorded.py"))  # must not raise


# ---------------------------------------------------------- clear


@pytest.mark.parametrize("cache_fixture", ["in_mem", "disk"])
def test_clear_drops_everything(cache_fixture, request):
    cache = request.getfixturevalue(cache_fixture)
    cache.record(_entry(file="a.py"))
    cache.record(_entry(file="b.py"))
    cache.clear()
    assert cache.lookup(Path("a.py"), (10, 50)) is None
    assert cache.lookup(Path("b.py"), (10, 50)) is None


# ---------------------------------------------------------- null cache semantics


def test_null_cache_never_records(null):
    null.record(_entry())
    assert null.lookup(Path("src/foo.py"), (10, 50)) is None


def test_null_cache_invalidate_and_clear_are_noop(null):
    null.invalidate(Path("anything.py"))
    null.clear()


# ---------------------------------------------------------- disk persistence


def test_disk_persists_across_instances(tmp_path):
    """A fresh DiskReadCache with same session_id sees previous entries."""
    c1 = DiskReadCache(session_id="persist-test", project_root=tmp_path)
    c1.record(_entry(h="persisted"))

    c2 = DiskReadCache(session_id="persist-test", project_root=tmp_path)
    got = c2.lookup(Path("src/foo.py"), (10, 50))
    assert got is not None
    assert got.content_hash == "persisted"


def test_disk_sessions_are_isolated(tmp_path):
    c_a = DiskReadCache(session_id="session-a", project_root=tmp_path)
    c_b = DiskReadCache(session_id="session-b", project_root=tmp_path)
    c_a.record(_entry(h="from-a"))
    assert c_b.lookup(Path("src/foo.py"), (10, 50)) is None


def test_disk_tolerates_corrupt_file(tmp_path):
    cache = DiskReadCache(session_id="corrupt-test", project_root=tmp_path)
    cache.path.write_text("not valid json {{{")
    # Lookup on corrupt file returns None rather than raising.
    assert cache.lookup(Path("src/foo.py"), (10, 50)) is None
    # Record overwrites the corrupt file.
    cache.record(_entry(h="recovered"))
    got = cache.lookup(Path("src/foo.py"), (10, 50))
    assert got is not None
    assert got.content_hash == "recovered"


def test_disk_clear_removes_file(tmp_path):
    cache = DiskReadCache(session_id="clear-test", project_root=tmp_path)
    cache.record(_entry())
    assert cache.path.exists()
    cache.clear()
    assert not cache.path.exists()


# ---------------------------------------------------------- selector


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SYMBOL_MCP_SESSION", raising=False)
    monkeypatch.delenv("SYMBOL_SESSION_ID", raising=False)


def test_build_read_cache_null_when_no_env():
    assert isinstance(build_read_cache(), NullReadCache)


def test_build_read_cache_disk_when_session_id(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYMBOL_SESSION_ID", "shell-123")
    assert isinstance(build_read_cache(), DiskReadCache)


def test_build_read_cache_in_memory_when_mcp(monkeypatch):
    monkeypatch.setenv("SYMBOL_MCP_SESSION", "1")
    assert isinstance(build_read_cache(), InMemoryReadCache)


def test_build_read_cache_mcp_wins_over_session_id(monkeypatch):
    monkeypatch.setenv("SYMBOL_MCP_SESSION", "1")
    monkeypatch.setenv("SYMBOL_SESSION_ID", "shell-123")
    # MCP implies long-lived process; in-memory is the right choice.
    assert isinstance(build_read_cache(), InMemoryReadCache)
