"""Read cache protocol.

Tracks bytes served to an agent via `symbol code` / `symbol search` / `symbol outline`
so that `symbol patch` can verify the agent saw the target content without
forcing a re-read.

Four methods: record, lookup, invalidate, clear. Backings vary — in-memory
for MCP long-lived processes, disk for CLI across process boundaries,
null for ad-hoc shell use with no session.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CachedRead:
    """A record of bytes we served to an agent."""

    file: str                   # repo-relative path
    byte_range: tuple[int, int] # end exclusive
    content_hash: str           # short sha256 of served bytes
    served_at: float            # wall time (unix seconds)
    served_mtime: float         # file mtime at serve time
    tool_call_idx: int          # session-local counter, for LRU


@runtime_checkable
class ReadCache(Protocol):
    """Tracks served content. Implementations decide storage backing."""

    def record(self, entry: CachedRead) -> None:
        """Upsert an entry. Later records for the same (file, range) overwrite."""
        ...

    def lookup(self, file: Path, byte_range: tuple[int, int]) -> CachedRead | None:
        """Return the most recent record for this exact range, or None."""
        ...

    def find_covering(
        self, file: Path, byte_range: tuple[int, int]
    ) -> CachedRead | None:
        """Return a cached entry whose byte range contains the requested range.

        Used by `symbol patch`: if agent read lines 10-50 (bytes 100-500) and
        now wants to patch bytes 150-200, the larger cached entry covers
        it. Returns the most recently recorded covering entry.
        """
        ...

    def invalidate(self, file: Path) -> None:
        """Drop all entries for a file. Called after writes."""
        ...

    def clear(self) -> None:
        """Drop everything. Called on session end."""
        ...
