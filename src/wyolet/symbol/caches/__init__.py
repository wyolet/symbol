"""Read cache implementations.

Pick the backing for the current execution context:
- MCP (long-lived process) → InMemoryReadCache.
- CLI with CA_SESSION_ID → DiskReadCache.
- Everything else → NullReadCache (forces needs_read_confirmation on every patch).
"""

import os

from wyolet.symbol.caches.disk import DiskReadCache
from wyolet.symbol.caches.in_memory import InMemoryReadCache
from wyolet.symbol.caches.instrument import record_served
from wyolet.symbol.caches.null import NullReadCache
from wyolet.symbol.protocols import ReadCache

__all__ = [
    "DiskReadCache",
    "InMemoryReadCache",
    "NullReadCache",
    "build_read_cache",
    "record_served",
]


def build_read_cache() -> ReadCache:
    """Select the right backing for this execution context."""
    if os.environ.get("CA_MCP_SESSION"):
        return InMemoryReadCache()
    session_id = os.environ.get("CA_SESSION_ID")
    if session_id:
        return DiskReadCache(session_id=session_id)
    return NullReadCache()
