"""`outline` read — parent-child tree of symbols.

Accepts either:
- a file path (repo-relative): returns every top-level symbol in that file
  with children nested.
- a symbol path (e.g. "UserService" or "ca.symbol.shared.SymbolIndex"):
  returns the matching symbol plus all its descendants.
"""

from ca.symbol.shared.symbol import S_SLINE
from ca.symbol.shared.symbol_index import SymbolIndex


def outline_file(index: SymbolIndex, file: str) -> list[dict]:
    """Every top-level symbol in `file`, with children nested."""
    if not index._built:
        index.build()

    file_id = index._file_ids.get(file)
    if file_id is None:
        return []

    row_ids = sorted(
        index.by_file.get(file_id, []),
        key=lambda i: index.symbols[i][S_SLINE],
    )
    return index.build_tree(row_ids)


def outline_symbol(index: SymbolIndex, query: str) -> list[dict]:
    """Matching symbol(s) with all descendants nested."""
    if not index._built:
        index.build()

    row_ids: list[int] = list(index.by_path.get(query, []))
    if not row_ids:
        suffix = "." + query
        for path, ids in index.by_path.items():
            if path == query or path.endswith(suffix):
                row_ids.extend(ids)
    if not row_ids:
        return []

    out: list[dict] = []
    for root_row in row_ids:
        descendants = [root_row, *index.descendants_of(root_row)]
        descendants.sort(key=lambda i: index.symbols[i][S_SLINE])
        nodes = index.build_tree(descendants, root_row=root_row)
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
