"""`search` query — narrow candidates by name. Returns signature + preview only.

Used when the agent is exploring: "is there a `save` in this codebase?"
Returns a lightweight hit list so the agent can pick by signature, then
retrieve the exact code with `ca code`.
"""

from ca_tools.shared.symbol_index import SymbolIndex


_PREVIEW_LINES = 2


def search(
    index: SymbolIndex,
    query: str,
    *,
    kind: str | None = None,
    file: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Fuzzy candidate list. Matches exact or suffix on the qualified path."""
    if not index._built:
        index.build()

    row_ids: list[int] = list(index.by_path.get(query, []))
    if not row_ids:
        suffix = "." + query
        for path, ids in index.by_path.items():
            if path == query or path.endswith(suffix):
                row_ids.extend(ids)

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


def _preview(index: SymbolIndex, row: int) -> str:
    """First 1-2 non-blank body lines after the signature.

    Gives the agent a feel for the body without paying body-length tokens.
    For a documented function, this tends to be the docstring's first line.
    """
    body = index.body(row)
    lines = body.splitlines()

    # Drop signature lines — everything up to and including the first line
    # that ends with a colon at paren-depth 0. We already computed the
    # signature, so just find where it ends in the body.
    sig = index.signature(row)
    # Count how many body lines we used for the signature.
    joined = ""
    consumed = 0
    for i, line in enumerate(lines):
        joined = (joined + " " + line.strip()).strip()
        if joined.replace(" ", "").endswith(sig.replace(" ", "")):
            consumed = i + 1
            break
    body_lines = lines[consumed:]

    preview: list[str] = []
    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            if preview:
                break
            continue
        preview.append(stripped[:120])
        if len(preview) >= _PREVIEW_LINES:
            break
    return " · ".join(preview)
