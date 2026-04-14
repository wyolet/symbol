"""Checker modules — imported to register checkers via decorators."""

from ca_tools.checkers import (  # noqa: F401
    code_structure,
    entrypoints,
    side_effects,
    stack,
    swallowed,
    todos,
    unused_deps,
)
