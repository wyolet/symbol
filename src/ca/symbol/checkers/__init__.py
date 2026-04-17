"""Checker modules — imported to register checkers via decorators."""

from ca.symbol.checkers import (  # noqa: F401
    code_structure,
    entrypoints,
    orphans,
    side_effects,
    stack,
    swallowed,
    todos,
    unused_deps,
)
