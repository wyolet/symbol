"""Neutral index-side queries adapters can call without importing the index.

Adapters need to walk inheritance / look up declarations / locate files
when resolving receivers during rename. They cannot import the symbol
index (that would couple the adapter to the indexing layer). Instead the
renamer passes them an `IndexQuery` — a thin neutral interface holding
the few queries the algorithm actually needs.

Keep this protocol minimal. Every method added here is a new coupling
that every future index implementation must satisfy.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from wyolet.symbol.protocols.types import SymbolPath


@runtime_checkable
class IndexQuery(Protocol):
    """Index-side queries for rename receiver resolution."""

    def find_declaration(
        self, qpath: SymbolPath,
    ) -> tuple[Path, str, tuple[int, int], str] | None:
        """Locate a symbol by qualified path.

        Returns (file_abs_path, file_rel_path, byte_range, kind) or None.
        """
        ...

    def class_bases(self, class_qpath: SymbolPath) -> tuple[SymbolPath, ...]:
        """Direct base classes of a class, as qualified paths.

        Used by adapters to walk MRO during member rename. Empty tuple
        for unknown classes or classes with no resolved bases.
        """
        ...

    def owners_of_leaf(self, leaf: str) -> tuple[SymbolPath, ...]:
        """Qualified paths of every declaration whose leaf equals `leaf`.

        Drives the rename fast path: if `len(owners_of_leaf) == 1`, the
        leaf is globally unique and discrimination is unnecessary.
        """
        ...
