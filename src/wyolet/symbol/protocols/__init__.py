"""Language-agnostic protocols for parsing and semantic analysis.

The pipeline and command resolvers never import language-specific modules
(ast, tree_sitter, etc.) directly. They only call through these protocols.
"""

from wyolet.symbol.protocols.language import (
    LanguageAdapter,
    SemanticLanguageAdapter,
)
from wyolet.symbol.protocols.read_cache import CachedRead, ReadCache
from wyolet.symbol.protocols.types import (
    BindingResolution,
    ByteRewrite,
    FileScan,
    ParseResult,
    RawImport,
    RawRef,
    RawSymbol,
    ReferenceResult,
    RenameAnalysis,
    ScannedImport,
    ScannedRef,
    ScannedSymbol,
    SkippedMismatchSite,
    SymbolPath,
    UnresolvedSite,
)

__all__ = [
    "BindingResolution",
    "ByteRewrite",
    "CachedRead",
    "FileScan",
    "IndexQuery",
    "LanguageAdapter",
    "ParseResult",
    "RawImport",
    "RawRef",
    "RawSymbol",
    "ReadCache",
    "ReferenceResult",
    "RenameAnalysis",
    "ScannedImport",
    "ScannedRef",
    "ScannedSymbol",
    "SemanticLanguageAdapter",
    "SkippedMismatchSite",
    "SymbolPath",
    "UnresolvedSite",
]


from wyolet.symbol.protocols.index_query import IndexQuery  # noqa: E402
