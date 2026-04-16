"""Read cache that records nothing.

Used in ad-hoc shell contexts where no session is configured. Every patch
will fall through to `needs_read_confirmation` because nothing ever matches
a lookup.
"""

from pathlib import Path

from ca_tools.protocols import CachedRead


class NullReadCache:
    """Black-hole cache. Safe default when no session identifies the caller."""

    def record(self, entry: CachedRead) -> None:
        return

    def lookup(self, file: Path, byte_range: tuple[int, int]) -> CachedRead | None:
        return None

    def invalidate(self, file: Path) -> None:
        return

    def clear(self) -> None:
        return
