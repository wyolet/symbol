"""Concrete IndexQuery adapter over SymbolIndex.

Implements the neutral IndexQuery protocol so language adapters can ask
the index a small number of questions during rename without importing
the index itself.
"""

from pathlib import Path

from wyolet.symbol.protocols import IndexQuery
from wyolet.symbol.protocols.types import SymbolPath
from wyolet.symbol.shared.symbol_index import SymbolIndex


class RenamerIndexQuery(IndexQuery):
    def __init__(self, index: SymbolIndex, project_root: Path):
        self.index = index
        self.project_root = project_root

    def find_declaration(
        self, qpath: SymbolPath,
    ) -> tuple[Path, str, tuple[int, int], str] | None:
        rows = self.index.by_path.get(qpath, [])
        if not rows:
            return None
        row = rows[0]
        rel = self.index.file_of(row)
        abs_path = self.project_root / rel
        return abs_path, rel, self.index.byte_range_of(row), self.index.kind_of(row)

    def class_bases(self, class_qpath: SymbolPath) -> tuple[SymbolPath, ...]:
        # TODO: index doesn't expose class bases yet. MRO walk lands with
        # task #4 once we add `bases_of` to the index builder.
        return ()

    def owners_of_leaf(self, leaf: str) -> tuple[SymbolPath, ...]:
        suffix = f".{leaf}"
        return tuple(
            qp for qp in self.index.by_path
            if qp == leaf or qp.endswith(suffix)
        )
