"""Read cache implementations.

Pick the backing for the current execution context:
- MCP (long-lived process) → InMemoryReadCache.
- CLI with CA_SESSION_ID → DiskReadCache.
- Everything else → NullReadCache (forces needs_read_confirmation on every patch).
"""

import os

from ca_tools.caches.disk import DiskReadCache
from ca_tools.caches.in_memory import InMemoryReadCache
from ca_tools.caches.instrument import record_served
from ca_tools.caches.null import NullReadCache
from ca_tools.protocols import ReadCache

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
