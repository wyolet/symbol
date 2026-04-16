"""`code` read — retrieve the exact body at a known address.

Two ways to address:
1. ``file:start-end`` — file path + inclusive line range
2. ``qualified.path`` — fully qualified symbol path as returned by `search`

For ambiguous or unknown input, the read refuses and tells the caller to
use ``ca search`` first. The contract: ``code`` never guesses.
"""

import re
from pathlib import Path

from ca_tools.shared.symbol_index import SymbolIndex


_RANGE_RE = re.compile(r"^(?P<file>.+?):(?P<start>\d+)-(?P<end>\d+)$")


class CodeAmbiguous(Exception):
    """Raised when the target resolves to more than one symbol."""

    def __init__(self, candidates: list[dict]):
        super().__init__(f"{len(candidates)} candidates — use ca search")
        self.candidates = candidates


class CodeNotFound(Exception):
    pass


def code(index: SymbolIndex, target: str) -> dict:
    """Return the body + imports + refs for an exact address.

    Raises CodeNotFound if nothing matches, CodeAmbiguous if the path
    resolves to multiple rows (agent must pass ``file:range`` instead).
    """
    if not index._built:
        index.build()

    m = _RANGE_RE.match(target)
    if m:
        return _by_range(index, m["file"], int(m["start"]), int(m["end"]))

    return _by_path(index, target)


def _by_path(index: SymbolIndex, path: str) -> dict:
    rows = list(index.by_path.get(path, []))
    if not rows:
        suffix = "." + path
        for qpath, ids in index.by_path.items():
            if qpath == path or qpath.endswith(suffix):
                rows.extend(ids)

    if not rows:
        raise CodeNotFound(f"no symbol matches {path!r}")

    if len(rows) > 1:
        raise CodeAmbiguous(
            [
                {
                    "path": index.path_of(r),
                    "file": index.file_of(r),
                    "start_line": index.range_of(r)[0],
                    "end_line": index.range_of(r)[1],
                    "kind": index.kind_of(r),
                    "signature": index.signature(r),
                }
                for r in rows
            ]
        )

    return index.row_payload(rows[0])


def _by_range(index: SymbolIndex, file: str, start: int, end: int) -> dict:
    file_id = index._file_ids.get(file)
    if file_id is None:
        as_path = Path(file)
        if as_path.exists():
            try:
                rel = str(as_path.resolve().relative_to(index.project_root))
                file_id = index._file_ids.get(rel)
                if file_id is not None:
                    file = rel
            except ValueError:
                file_id = None

    if file_id is None:
        raise CodeNotFound(f"file not indexed: {file!r}")

    best_row = -1
    best_span = None
    for row in index.by_file.get(file_id, []):
        rs, re_ = index.range_of(row)
        if rs == start and re_ == end:
            best_row = row
            best_span = (rs, re_)
            break
        if rs <= start and re_ >= end:
            span = re_ - rs
            if best_span is None or span < (best_span[1] - best_span[0]):
                best_row = row
                best_span = (rs, re_)

    if best_row == -1:
        return index.raw_slice(file, start, end)

    payload = index.row_payload(best_row)
    if payload["start_line"] != start or payload["end_line"] != end:
        payload["note"] = (
            f"requested {file}:{start}-{end} is inside "
            f"{payload['path']} ({payload['start_line']}-{payload['end_line']})"
        )
    return payload
