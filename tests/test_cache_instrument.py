"""Tests for cache instrumentation helpers."""

import hashlib
import time
from pathlib import Path

from ca_tools.caches import InMemoryReadCache, record_served


def _write(path: Path, content: str) -> None:
    path.write_text(content)


def test_record_served_writes_expected_entry(tmp_path):
    src = tmp_path / "foo.py"
    content = "a = 1\nb = 2\nc = 3\nd = 4\n"
    _write(src, content)

    cache = InMemoryReadCache()
    record_served(
        cache,
        project_root=tmp_path,
        file_rel="foo.py",
        start_line=2,
        end_line=3,
    )

    # byte range of lines 2-3 is "b = 2\nc = 3\n" → bytes 6..18
    got = cache.lookup(Path("foo.py"), (6, 18))
    assert got is not None
    served = b"b = 2\nc = 3\n"
    assert got.content_hash == hashlib.sha256(served).hexdigest()[:16]


def test_record_served_first_line(tmp_path):
    src = tmp_path / "foo.py"
    _write(src, "first\nsecond\n")

    cache = InMemoryReadCache()
    record_served(
        cache, project_root=tmp_path, file_rel="foo.py", start_line=1, end_line=1
    )

    got = cache.lookup(Path("foo.py"), (0, 6))
    assert got is not None
    assert got.content_hash == hashlib.sha256(b"first\n").hexdigest()[:16]


def test_record_served_last_line_no_trailing_newline(tmp_path):
    src = tmp_path / "foo.py"
    _write(src, "a\nb\nc")  # no trailing newline

    cache = InMemoryReadCache()
    record_served(
        cache, project_root=tmp_path, file_rel="foo.py", start_line=3, end_line=3
    )

    got = cache.lookup(Path("foo.py"), (4, 5))
    assert got is not None
    assert got.content_hash == hashlib.sha256(b"c").hexdigest()[:16]


def test_record_served_captures_mtime(tmp_path):
    src = tmp_path / "foo.py"
    _write(src, "x = 1\n")

    cache = InMemoryReadCache()
    before = time.time()
    record_served(
        cache, project_root=tmp_path, file_rel="foo.py", start_line=1, end_line=1
    )
    after = time.time()

    got = cache.lookup(Path("foo.py"), (0, 6))
    assert got is not None
    assert before - 1 <= got.served_mtime <= after + 1


def test_record_served_missing_file_is_silent(tmp_path):
    """Unreadable file must not raise — read commands can't be broken by cache."""
    cache = InMemoryReadCache()
    record_served(
        cache,
        project_root=tmp_path,
        file_rel="does-not-exist.py",
        start_line=1,
        end_line=1,
    )
    # Nothing recorded, nothing raised.
    assert cache.lookup(Path("does-not-exist.py"), (0, 0)) is None
