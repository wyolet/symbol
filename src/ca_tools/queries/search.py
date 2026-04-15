"""`search` query — narrow candidates by name. Returns signature + preview only.

Used when the agent is exploring: "is there a `save` in this codebase?"
Returns a lightweight hit list so the agent can pick by signature, then
retrieve the exact code with `ca code`.
"""

import re

from ca_tools.shared.symbol_index import SymbolIndex


_PREVIEW_LINES = 3


def search(
    index: SymbolIndex,
    patterns: list[str] | str,
    *,
    kind: str | None = None,
    file: str | None = None,
    regex: bool = False,
    fixed: bool = False,
    ignore_case: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Candidate list. Multiple patterns AND together.

    Default: exact match or dotted-suffix on qualified path.
    --regex: each pattern is a Python regex (unanchored, re.search).
    --fixed: each pattern is a literal substring.
    """
    if not index._built:
        index.build()

    if isinstance(patterns, str):
        patterns = [patterns]
    if not patterns:
        return []

    matchers = [_compile(p, regex=regex, fixed=fixed, ignore_case=ignore_case) for p in patterns]

    row_ids: list[int] = []
    seen: set[int] = set()
    for path, ids in index.by_path.items():
        if not all(m(path) for m in matchers):
            continue
        for rid in ids:
            if rid not in seen:
                seen.add(rid)
                row_ids.append(rid)

    out: list[dict] = []
    for row in row_ids:
        if kind is not None and index.kind_of(row) != kind:
            continue
        file_path = index.file_of(row)
        if file is not None and file != file_path:
            continue
        s, e = index.range_of(row)
        out.append(
            {
                "path": index.path_of(row),
                "file": file_path,
                "start_line": s,
                "end_line": e,
                "kind": index.kind_of(row),
                "language": index.language_of(row),
                "signature": index.signature(row),
                "preview": _preview(index, row),
            }
        )
        if len(out) >= limit:
            break
    return out


def _compile(pattern: str, *, regex: bool, fixed: bool, ignore_case: bool):
    """Return a predicate (str) -> bool for one pattern."""
    if regex:
        flags = re.IGNORECASE if ignore_case else 0
        rx = re.compile(pattern, flags)
        return lambda s: rx.search(s) is not None
    if fixed:
        if ignore_case:
            needle = pattern.lower()
            return lambda s: needle in s.lower()
        return lambda s: pattern in s

    # Default: exact or dotted-suffix match (legacy behavior).
    suffix = "." + pattern
    if ignore_case:
        target = pattern.lower()
        suffix_l = suffix.lower()
        return lambda s: s.lower() == target or s.lower().endswith(suffix_l)
    return lambda s: s == pattern or s.endswith(suffix)


def _preview(index: SymbolIndex, row: int) -> str:
    """First few non-blank code lines after the signature, skipping docstrings and comments.

    Returns empty string if the body has no meaningful code lines.
    """
    body = index.body(row)
    lines = body.splitlines()

    sig = index.signature(row)
    joined = ""
    consumed = 0
    for i, line in enumerate(lines):
        joined = (joined + " " + line.strip()).strip()
        if joined.replace(" ", "").endswith(sig.replace(" ", "")):
            consumed = i + 1
            break
    body_lines = lines[consumed:]

    # Skip leading docstring
    i = 0
    while i < len(body_lines) and not body_lines[i].strip():
        i += 1
    if i < len(body_lines):
        stripped = body_lines[i].strip()
        for quote in ('"""', "'''", '"', "'"):
            if stripped.startswith(quote):
                # Find closing quote
                if stripped.count(quote) >= 2 and len(stripped) > len(quote):
                    i += 1
                else:
                    i += 1
                    while i < len(body_lines) and quote not in body_lines[i]:
                        i += 1
                    i += 1
                break

    # Find first non-blank/non-comment line, then take N lines from there
    # preserving original indentation relative to that line.
    start = None
    for j, line in enumerate(body_lines[i:], start=i):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        start = j
        break
    if start is None:
        return ""

    picked = body_lines[start : start + _PREVIEW_LINES]
    base_indent = len(picked[0]) - len(picked[0].lstrip())
    return "\n".join(line[base_indent:] if len(line) >= base_indent else line for line in picked)
