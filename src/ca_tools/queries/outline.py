"""`outline` query — parent-child tree of symbols.

Accepts either:
- a file path (repo-relative): returns every top-level symbol in that file
  with children nested.
- a symbol path (e.g. "UserService" or "ca_tools.shared.SymbolIndex"):
  returns the matching symbol plus all its descendants.
"""

from ca_tools.shared.symbol_index import SymbolIndex


def outline_file(index: SymbolIndex, file: str) -> list[dict]:
    """Every top-level symbol in `file`, with children nested."""
    if not index._built:
        index.build()

    file_id = index._file_ids.get(file)
    if file_id is None:
        return []

    row_ids = sorted(
        index.by_file.get(file_id, []),
        key=lambda i: index.symbols[i][2],  # S_SLINE
    )
    return _build_tree(index, row_ids)


def outline_symbol(index: SymbolIndex, query: str) -> list[dict]:
    """Matching symbol(s) with all descendants nested."""
    if not index._built:
        index.build()

    # Exact match first; fall back to suffix.
    row_ids: list[int] = list(index.by_path.get(query, []))
    if not row_ids:
        suffix = "." + query
        for path, ids in index.by_path.items():
            if path == query or path.endswith(suffix):
                row_ids.extend(ids)
    if not row_ids:
        return []

    # For each match, collect the symbol + all descendants by parent chain.
    out: list[dict] = []
    for root_row in row_ids:
        descendants = [root_row]
        descendants.extend(_collect_descendants(index, root_row))
        descendants.sort(key=lambda i: index.symbols[i][2])
        nodes = _build_tree(index, descendants, root_row=root_row)
        out.extend(nodes)
    return out


def outline(index: SymbolIndex, query: str) -> list[dict]:
    """Dispatch on whether `query` is a file path or symbol path.

    Heuristic: file paths contain '/' or end in a known source extension.
    Everything else is a symbol name. If a query matches both, file wins.
    """
    if not index._built:
        index.build()

    if query in index._file_ids:
        return outline_file(index, query)

    if "/" in query or query.endswith((".py", ".ts", ".js", ".go", ".php")):
        return outline_file(index, query)

    return outline_symbol(index, query)


def _collect_descendants(index: SymbolIndex, root: int) -> list[int]:
    out: list[int] = []
    stack = [root]
    seen = {root}
    while stack:
        parent = stack.pop()
        for i, sym in enumerate(index.symbols):
            if sym[8] == parent and i not in seen:  # S_PARENT
                seen.add(i)
                out.append(i)
                stack.append(i)
    return out


def _build_tree(
    index: SymbolIndex,
    row_ids: list[int],
    root_row: int | None = None,
) -> list[dict]:
    nodes: dict[int, dict] = {}
    roots: list[dict] = []
    for row in row_ids:
        s, e = index.range_of(row)
        node = {
            "path": index.path_of(row),
            "kind": index.kind_of(row),
            "signature": index.signature(row),
            "start_line": s,
            "end_line": e,
            "children": [],
        }
        nodes[row] = node
        parent_row = index.parent_of(row)
        parent = nodes.get(parent_row)
        if parent is None or row == root_row:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots
