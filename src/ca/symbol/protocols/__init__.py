"""Language-agnostic protocols for parsing and semantic analysis.

The pipeline and command resolvers never import language-specific modules
(ast, tree_sitter, etc.) directly. They only call through these protocols.
"""

from ca.symbol.protocols.language import (
    LanguageAdapter,
    SemanticLanguageAdapter,
)
from ca.symbol.protocols.read_cache import CachedRead, ReadCache
from ca.symbol.protocols.types import (
    BindingResolution,
    FileScan,
    ParseResult,
    RawImport,
    RawRef,
    RawSymbol,
    ReferenceResult,
    ScannedImport,
    ScannedRef,
    ScannedSymbol,
    SymbolPath,
)

__all__ = [
    "BindingResolution",
    "CachedRead",
    "FileScan",
    "LanguageAdapter",
    "ParseResult",
    "RawImport",
    "RawRef",
    "RawSymbol",
    "ReadCache",
    "ReferenceResult",
    "ScannedImport",
    "ScannedRef",
    "ScannedSymbol",
    "SemanticLanguageAdapter",
    "SymbolPath",
]
