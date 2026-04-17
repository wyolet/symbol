"""`callers` read — tier-1 name-based reference scan."""

from ca.symbol.shared.symbol_index import SymbolIndex


def callers(index: SymbolIndex, name: str) -> list[dict]:
    """Return every symbol whose body references `name`.

    Tier 1: matches last name segment textually; unresolved. A call to
    `other.save` matches when asking for `save`. Output format is stable;
    consumers should trust `name` + `kind` + `line` as truth about the
    reference, and read the containing symbol's body for disambiguation.
    """
    if not index._built:
        index.build()

    target = name.rsplit(".", 1)[-1]
    out: list[dict] = []
    for source_row, line, kind in index.callers_of(target):
        s, e = index.range_of(source_row)
        out.append(
            {
                "source_path": index.path_of(source_row),
                "source_file": index.file_of(source_row),
                "source_range": [s, e],
                "ref_name": target,
                "ref_kind": kind,
                "ref_line": line,
            }
        )
    return out
