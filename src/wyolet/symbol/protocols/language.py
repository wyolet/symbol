"""Language adapter protocols.

Two protocols define what it means to "understand" a language:

- LanguageAdapter: syntactic parsing + enumeration. Every backend implements
  this — native parsers (Python ast), tree-sitter, subprocess wrappers.

- SemanticLanguageAdapter: extends with binding resolution. Only adapters
  that do scope-aware analysis implement this (pyright/jedi/gopls-based, and
  our own ast-plus-analysis PythonAstAdapter for module-scope cases).

Protocol classes (not ABC) for structural typing — adapters wrapping third-
party libraries don't need to inherit from our base.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from wyolet.symbol.protocols.types import (
    BindingResolution,
    FileScan,
    ParseResult,
    RawImport,
    RawRef,
    RawSymbol,
    ReferenceResult,
    SymbolPath,
)


@runtime_checkable
class LanguageAdapter(Protocol):
    """Syntactic parsing and enumeration for one language.

    Methods take (path, source bytes). The adapter is free to cache parse
    trees internally keyed on path/hash/mtime; callers do not see trees.
    """

    lang: str
    """Canonical language id (e.g. 'python', 'typescript'). Matches linguist output."""

    is_enabled: bool
    """Whether this adapter can run in the current environment.

    In-process adapters with no toolchain dependency (e.g. PythonAstAdapter)
    set this to ``True``. Daemon/LSP-backed adapters check for their binary
    or server. ``LanguageRegistry.for_language`` consults this and falls
    through to the next-priority adapter when False.

    Each adapter's class docstring describes — in order — its tier,
    language, capabilities, what it requires, how to enable it, and what
    fallback (if any) kicks in when disabled. That docstring is the
    install contract surfaced by ``symbol doctor``.
    """

    def symbols(self, path: Path, source: bytes) -> list[RawSymbol]:
        """Nested tree of symbols declared in this file."""
        ...

    def imports(self, path: Path, source: bytes) -> list[RawImport]:
        """All import statements in this file."""
        ...

    def references_in(
        self, path: Path, source: bytes, symbol: RawSymbol
    ) -> list[RawRef]:
        """Name references inside a symbol's body. Textual-level, no resolution."""
        ...

    def validate_syntax(self, source: bytes) -> ParseResult:
        """Check whether bytes form a syntactically valid document."""
        ...

    def module_prefix(self, path: Path, project_root: Path) -> str:
        """Symbol-qualifier prefix for a source file.

        Receives the absolute file path and the project root. The adapter
        is free to walk the filesystem from ``path`` (e.g. Go reads the
        nearest ``go.mod``) or compute from layout (e.g. Python's
        ``a/b/c.py`` → ``a.b.c`` with ``src/`` stripped and ``__init__``
        flattened).
        """
        ...

    def signature_from_text(self, text: str) -> str:
        """Extract the declaration line(s) of a symbol from its body text.

        Receives up to a few KB of bytes starting at the symbol's first byte.
        Returns the declaration up to (and including) the body delimiter:
        ``:`` for Python, ``{`` for Go, etc. Multi-line signatures collapse
        to one line. Pure function — no I/O.
        """
        ...

    def scan_file(self, path: Path, source: bytes) -> FileScan:
        """Single-call scan for the symbol index builder.

        Returns language + imports (per-binding) + symbols (nested tree,
        each carrying its own refs). Drives index construction; write
        engines use the piecewise methods above instead.
        """
        ...

    def invalidate(self, path: Path) -> None:
        """Drop any cached state for this path. Called after writes."""
        ...


@runtime_checkable
class SemanticLanguageAdapter(LanguageAdapter, Protocol):
    """Semantic analysis: name binding and reverse reference lookup.

    Methods may report `unsupported=True` for cases the adapter cannot
    handle (e.g. local scope resolution when the adapter only does
    module-scope binding). Callers surface the `reason` field in user-
    facing errors as `unsupported_operation`.
    """

    def resolve_binding(
        self, path: Path, source: bytes, line: int, name: str
    ) -> BindingResolution:
        """Resolve `name` at `(path, line)` to the symbol it binds to."""
        ...

    def references_to(self, symbol: SymbolPath) -> ReferenceResult:
        """All references to a symbol, across the project."""
        ...
