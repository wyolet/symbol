"""Language adapter implementations.

Each adapter wraps a parsing/analysis backend and implements the protocols
defined in `ca_tools.protocols`. These are the only modules in the codebase
that are allowed to import language-specific libraries (ast, tree_sitter,
pyright client, etc.).
"""

from ca_tools.adapters.python_ast import PythonAstAdapter

__all__ = ["PythonAstAdapter"]
