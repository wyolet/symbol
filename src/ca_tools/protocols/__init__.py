"""Language-agnostic protocols for parsing and semantic analysis.

The pipeline and command resolvers never import language-specific modules
(ast, tree_sitter, etc.) directly. They only call through these protocols.
"""

from ca_tools.protocols.language import (
    LanguageAdapter,
    SemanticLanguageAdapter,
)
from ca_tools.protocols.read_cache import CachedRead, ReadCache
from ca_tools.protocols.types import (
    BindingResolution,
    ParseResult,
    RawImport,
    RawRef,
    RawSymbol,
    ReferenceResult,
    SymbolPath,
)

__all__ = [
    "BindingResolution",
    "CachedRead",
    "LanguageAdapter",
    "ParseResult",
    "RawImport",
    "RawRef",
    "RawSymbol",
    "ReadCache",
    "ReferenceResult",
    "SemanticLanguageAdapter",
    "SymbolPath",
]
