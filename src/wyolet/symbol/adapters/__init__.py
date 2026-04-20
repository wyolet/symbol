"""Language adapter implementations.

Each adapter wraps a parsing/analysis backend and implements the protocols
defined in `wyolet.symbol.protocols`. These are the only modules in the codebase
that are allowed to import language-specific libraries (ast, tree_sitter,
pyright client, etc.).
"""

from wyolet.symbol.adapters.python_ast import PythonAstAdapter
from wyolet.symbol.adapters.registry import (
    LanguageRegistry,
    UnsupportedLanguage,
    default_registry,
)

__all__ = [
    "LanguageRegistry",
    "PythonAstAdapter",
    "UnsupportedLanguage",
    "default_registry",
]
