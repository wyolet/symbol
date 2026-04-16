"""In-memory read cache for long-lived processes (MCP server)."""

import threading
from pathlib import Path

from ca_tools.protocols import CachedRead


class InMemoryReadCache:
    """Dict-backed cache scoped to one process / MCP session.

    Thread-safe for the MCP case where multiple tool calls may overlap.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (file, byte_range) -> CachedRead
        self._entries: dict[tuple[str, tuple[int, int]], CachedRead] = {}

    def record(self, entry: CachedRead) -> None:
        with self._lock:
            self._entries[(entry.file, entry.byte_range)] = entry

    def lookup(self, file: Path, byte_range: tuple[int, int]) -> CachedRead | None:
        key = (str(file), byte_range)
        with self._lock:
            return self._entries.get(key)

    def find_covering(
        self, file: Path, byte_range: tuple[int, int]
    ) -> CachedRead | None:
        target = str(file)
        req_start, req_end = byte_range
        best: CachedRead | None = None
        with self._lock:
            for (f, (s, e)), entry in self._entries.items():
                if f != target:
                    continue
                if s <= req_start and e >= req_end:
                    if best is None or entry.served_at > best.served_at:
                        best = entry
        return best

    def invalidate(self, file: Path) -> None:
        target = str(file)
        with self._lock:
            self._entries = {k: v for k, v in self._entries.items() if k[0] != target}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
