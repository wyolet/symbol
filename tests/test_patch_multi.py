"""Tests for apply_patch_multi: batched, atomic, line- or content-addressed."""

from pathlib import Path

import pytest

from wyolet.symbol.caches import InMemoryReadCache
from wyolet.symbol.protocols import CachedRead
from wyolet.symbol.writes.patch import apply_patch_multi


SOURCE = "alpha\nbeta\ngamma\ndelta\nepsilon\n"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "f.txt").write_text(SOURCE)
    return tmp_path


def _full_cache(file_abs: Path, file_rel: str) -> InMemoryReadCache:
    cache = InMemoryReadCache()
    data = file_abs.read_bytes()
    cache.record(CachedRead(
        file=file_rel, byte_range=(0, len(data)),
        content_hash="x", served_at=0, served_mtime=0, tool_call_idx=0,
    ))
    return cache


def test_single_range_edit(project):
    cache = _full_cache(project / "f.txt", "f.txt")
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [{"range": "2-2", "content": "BETA\n"}],
        cache=cache,
    )
    assert r.status == "applied"
    assert (project / "f.txt").read_text() == "alpha\nBETA\ngamma\ndelta\nepsilon\n"


def test_two_range_edits_atomic(project):
    cache = _full_cache(project / "f.txt", "f.txt")
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [
            {"range": "2-2", "content": "BETA\n"},
            {"range": "4-4", "content": "DELTA\n"},
        ],
        cache=cache,
    )
    assert r.status == "applied"
    assert (project / "f.txt").read_text() == "alpha\nBETA\ngamma\nDELTA\nepsilon\n"


def test_old_addressed_no_cache_needed(project):
    cache = InMemoryReadCache()  # empty — old mode skips check
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [{"old": "beta", "content": "BETA"}],
        cache=cache,
    )
    assert r.status == "applied"
    assert (project / "f.txt").read_text() == "alpha\nBETA\ngamma\ndelta\nepsilon\n"


def test_old_ambiguous_returns_line_numbers(project):
    (project / "f.txt").write_text("foo\nfoo\nfoo\n")
    cache = InMemoryReadCache()
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [{"old": "foo", "content": "BAR"}],
        cache=cache,
    )
    assert r.status == "error"
    assert r.error_code == "ambiguous"
    assert "[1, 2, 3]" in r.message


def test_old_not_found(project):
    cache = InMemoryReadCache()
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [{"old": "missing", "content": "X"}],
        cache=cache,
    )
    assert r.status == "error"
    assert r.error_code == "not_found"


def test_mixed_with_unconfirmed_range_aborts_whole_batch(project):
    cache = InMemoryReadCache()  # nothing cached
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [
            {"old": "alpha", "content": "ALPHA"},  # OK without cache
            {"range": "3-3", "content": "GAMMA\n"},  # needs cache
        ],
        cache=cache,
    )
    assert r.status == "needs_read_confirmation"
    assert len(r.unconfirmed) == 1
    assert r.unconfirmed[0]["range"] == "3-3"
    # File untouched.
    assert (project / "f.txt").read_text() == SOURCE


def test_overlapping_ranges_rejected(project):
    cache = _full_cache(project / "f.txt", "f.txt")
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [
            {"range": "2-3", "content": "X\n"},
            {"range": "3-4", "content": "Y\n"},
        ],
        cache=cache,
    )
    assert r.status == "error"
    assert r.error_code == "overlapping_edits"


def test_dry_run_does_not_write(project):
    cache = _full_cache(project / "f.txt", "f.txt")
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [{"range": "1-1", "content": "ALPHA\n"}],
        cache=cache, dry_run=True,
    )
    assert r.status == "dry_run"
    assert r.diff
    assert (project / "f.txt").read_text() == SOURCE


def test_diff_merges_nearby_hunks(project):
    """Nearby edits collapse into one hunk; distant edits get separate hunks."""
    long = "".join(f"line{i}\n" for i in range(1, 51))  # 50 lines
    (project / "f.txt").write_text(long)
    cache = _full_cache(project / "f.txt", "f.txt")
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [
            {"range": "5-5", "content": "FIVE\n"},
            {"range": "8-8", "content": "EIGHT\n"},  # within 5 lines → merges
            {"range": "45-45", "content": "FORTYFIVE\n"},  # far — separate hunk
        ],
        cache=cache, diff_context=3,
    )
    assert r.status == "applied"
    # Two hunks: one for 5+8 (merged), one for 45.
    assert r.diff.count("@@ -") == 2


def test_recaches_each_after_range(project):
    cache = _full_cache(project / "f.txt", "f.txt")
    r = apply_patch_multi(
        project / "f.txt", "f.txt",
        [
            {"range": "2-2", "content": "BETA\n"},
            {"range": "4-4", "content": "DELTA\n"},
        ],
        cache=cache,
    )
    assert r.status == "applied"
    for entry in r.per_edit:
        assert cache.find_covering(Path("f.txt"), entry["after_range"]) is not None
