"""Cache-recording helpers for read commands.

Read commands call `record_served(...)` at their response point to tell the
cache what bytes were shown to the agent. Patch later consults the cache
to decide whether to apply directly or prompt the agent to confirm.
"""

import hashlib
import os
import time
from pathlib import Path

from ca_tools.protocols import CachedRead, ReadCache


def record_served(
    cache: ReadCache,
    *,
    project_root: Path,
    file_rel: str,
    start_line: int,
    end_line: int,
) -> None:
    """Compute byte range + hash of the served lines and record in cache.

    Reads the file from disk once to compute the exact byte offsets. No-ops
    silently if the file is unreadable — we never want a cache miss to
    break a read command.
    """
    abs_path = project_root / file_rel
    try:
        data = abs_path.read_bytes()
        mtime = os.stat(abs_path).st_mtime
    except OSError:
        return

    start_byte = _line_start_byte(data, start_line)
    end_byte = _line_end_byte(data, end_line)
    served = data[start_byte:end_byte]

    entry = CachedRead(
        file=file_rel,
        byte_range=(start_byte, end_byte),
        content_hash=hashlib.sha256(served).hexdigest()[:16],
        served_at=time.time(),
        served_mtime=mtime,
        tool_call_idx=0,
    )
    cache.record(entry)


def _line_start_byte(data: bytes, line: int) -> int:
    """Byte offset of the start of `line` (1-indexed)."""
    if line <= 1:
        return 0
    seen = 1
    for i, b in enumerate(data):
        if b == 0x0A:
            seen += 1
            if seen == line:
                return i + 1
    return len(data)


def _line_end_byte(data: bytes, line: int) -> int:
    """Byte offset just past the end of `line` (inclusive end, exclusive byte)."""
    seen = 0
    for i, b in enumerate(data):
        if b == 0x0A:
            seen += 1
            if seen == line:
                return i + 1
    return len(data)
