"""Language adapter implementations.

These are the only modules in the codebase that are allowed to import
language-specific libraries (ast, etc.). Importing the package registers each
language's dependency-file parsers with the DEPS pipeline.
"""

from wyolet.symbol.adapters import python_deps  # noqa: F401
