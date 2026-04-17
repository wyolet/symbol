"""On-disk read cache for CLI sessions.

Persists across process invocations so multi-step agent sessions keep their
cache even though each `ca` call is a fresh process. Keyed on a session id
passed via the CA_SESSION_ID env var.

Storage is a single JSON file per session. The cache is small (hundreds of
entries at most), so we rewrite the whole file on every record — simpler
than any incremental scheme and fast enough.
"""

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from ca.symbol.protocols import CachedRead

_CACHE_DIR = Path(".ca") / "cache" / "sessions"


class DiskReadCache:
    """JSON-file-backed cache, keyed by CA_SESSION_ID."""

    def __init__(self, session_id: str, project_root: Path | None = None) -> None:
        self.session_id = session_id
        root = project_root or Path.cwd()
        self.path = root / _CACHE_DIR / f"{session_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- protocol

    def record(self, entry: CachedRead) -> None:
        entries = self._read()
        entries[_key(entry.file, entry.byte_range)] = asdict(entry)
        self._write(entries)

    def lookup(self, file: Path, byte_range: tuple[int, int]) -> CachedRead | None:
        entries = self._read()
        raw = entries.get(_key(str(file), byte_range))
        if raw is None:
            return None
        return _from_raw(raw)

    def find_covering(
        self, file: Path, byte_range: tuple[int, int]
    ) -> CachedRead | None:
        target = str(file)
        req_start, req_end = byte_range
        entries = self._read()
        best: CachedRead | None = None
        for raw in entries.values():
            if raw["file"] != target:
                continue
            s, e = raw["byte_range"][0], raw["byte_range"][1]
            if s <= req_start and e >= req_end:
                entry = _from_raw(raw)
                if best is None or entry.served_at > best.served_at:
                    best = entry
        return best

    def invalidate(self, file: Path) -> None:
        target = str(file)
        entries = self._read()
        entries = {k: v for k, v in entries.items() if v["file"] != target}
        self._write(entries)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    # ---------------------------------------------------------- storage

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted file → start fresh rather than raising.
            return {}

    def _write(self, entries: dict[str, dict]) -> None:
        # Atomic write: tmp file + rename.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(entries, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def _key(file: str, byte_range: tuple[int, int]) -> str:
    return f"{file}:{byte_range[0]}-{byte_range[1]}"


def _from_raw(raw: dict) -> CachedRead:
    """Rehydrate a CachedRead from JSON-deserialized dict (tuples become lists)."""
    raw = dict(raw)
    raw["byte_range"] = tuple(raw["byte_range"])
    return CachedRead(**raw)
