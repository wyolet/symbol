"""Concrete IndexQuery adapter over SymbolIndex.

Implements the neutral IndexQuery protocol so language adapters can ask
the index a small number of questions during rename without importing
the index itself.
"""

from pathlib import Path

from wyolet.symbol.protocols import IndexQuery
from wyolet.symbol.protocols.types import SymbolPath
from wyolet.symbol.shared.symbol import S_FILE
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
        """Resolve a class's base-expression strings to qualified paths.

        For each base captured on the class row, try same-file/same-module
        resolution first, then import resolution; unresolvable bases (stdlib,
        third-party, dynamic) are simply dropped. Order preserves the source
        base order for the resolvable subset.
        """
        rows = self.index.by_path.get(class_qpath)
        if not rows:
            return ()
        row = rows[0]
        file_id = self.index.symbols[row][S_FILE]
        # Ancestor scope segments (class_qpath minus its own leaf), used to
        # build same-module sibling candidates; e.g. "a.b.Sub" -> ["a", "b"].
        ancestors = class_qpath.split(".")[:-1]
        imports = self.index.imports_for(file_id)

        resolved: list[SymbolPath] = []
        for base in self.index.bases_for(row):
            qp = self._resolve_same_file(base, ancestors, file_id)
            if qp is None:
                qp = self._resolve_import(base, imports)
            if qp is not None:
                resolved.append(qp)
        return tuple(resolved)

    def _resolve_same_file(
        self, base: str, ancestors: list[str], file_id: int,
    ) -> SymbolPath | None:
        """Same-module sibling: replace the class leaf with `base` and walk
        the enclosing scopes outward, matching only rows in the same file.

        Handles nested classes — for ``mod.Outer.Inner(Base)`` this tries
        ``mod.Outer.Base`` then ``mod.Base`` then ``Base``.
        """
        for i in range(len(ancestors), -1, -1):
            candidate = ".".join(ancestors[:i] + [base])
            for r in self.index.by_path.get(candidate, []):
                if self.index.symbols[r][S_FILE] == file_id:
                    return candidate
        return None

    def _resolve_import(
        self, base: str, imports: list[tuple[str, str, int]],
    ) -> SymbolPath | None:
        """Import resolution: match the base's head segment to an import local.

        ``from x import Base`` (local=Base, source=x) + base ``Base`` -> ``x.Base``.
        ``import mod``         (local=mod, source=mod) + base ``mod.Base`` -> ``mod.Base``.
        ``import numpy as np`` (local=np, source=numpy) + base ``np.ndarray`` -> ``numpy.ndarray``.
        """
        head = base.split(".", 1)[0]
        tail = base[len(head):]  # "" for a bare name, ".rest" when dotted
        for local, source, _line in imports:
            if local != head:
                continue
            # Module imported under its own (possibly dotted) name: the base
            # string already spells the full qualified path.
            if base == source or base.startswith(source + "."):
                return base
            if source == head:
                return base
            # Aliased module reference: swap the alias head for the real module.
            if tail:
                return source + tail
            # from-import of the member itself.
            return f"{source}.{base}"
        return None

    def owners_of_leaf(self, leaf: str) -> tuple[SymbolPath, ...]:
        suffix = f".{leaf}"
        return tuple(
            qp for qp in self.index.by_path
            if qp == leaf or qp.endswith(suffix)
        )
