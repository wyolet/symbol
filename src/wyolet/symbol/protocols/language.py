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

from wyolet.symbol.protocols.index_query import IndexQuery
from wyolet.symbol.protocols.types import (
    BindingResolution,
    FileScan,
    ParseResult,
    RawImport,
    RawRef,
    RawSymbol,
    ReferenceResult,
    RenameAnalysis,
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

    def preview(self, body: str, signature: str, max_lines: int = 3) -> str:
        """First few meaningful body lines after the signature.

        Skips blank lines and language-appropriate comments (``#`` for
        Python, ``//`` for Go, …) and any docstring-equivalent at the top
        of the body. Returns up to ``max_lines`` lines joined by newlines,
        indentation preserved. Used by ``symbol search`` to show a few
        lines of context under each hit.
        """
        ...

    def signature(self, text: str) -> str:
        """Canonical declaration of the first top-level symbol in ``text``.

        Receives up to a few KB of bytes starting at the symbol's first
        byte. The adapter parses with its native AST and formats the
        declaration — no body, no trailing delimiter. Matches what
        gopls / godoc / inspect.signature would show.
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

    # ── rename support ──────────────────────────────────────────────
    # The adapter owns the full rename algorithm for each symbol kind:
    # find references, resolve receivers, classify each site as
    # rewrite / skipped_mismatch / unresolved. Returns a neutral
    # `RenameAnalysis` the engine then aggregates and commits.
    #
    # Uniform policy lives in the engine: any non-empty `unresolved`
    # aborts the operation unless force=True is passed in. Adapters
    # never decide to apply or abort — they only classify.

    def rename_module_binding(
        self,
        path: Path,
        project_root: Path,
        source: bytes,
        leaf: str,
        target_qpath: SymbolPath,
        target_module_qpath: SymbolPath,
        index: IndexQuery,
        is_declaring_file: bool,
        decl_byte_range: tuple[int, int] | None,
        new_name: str,
    ) -> RenameAnalysis:
        """Classify every reference to a module-level binding (function,
        async function, class, module constant) in this file.

        - `target_module_qpath`: qpath of the declaring module (e.g.
          "services.user" for a function at "services.user.foo").
        - References fall into three node shapes: `Name(id==leaf)` for
          bare references, `Attribute(attr==leaf)` for module-qualified
          access (e.g. `m.foo`), and `alias(name==leaf)` for imports.
        - For Attribute access, the receiver must resolve to
          `target_module_qpath`; mismatches go to skipped_mismatch.
        """
        ...

    def rename_member(
        self,
        path: Path,
        project_root: Path,
        source: bytes,
        leaf: str,
        target_qpath: SymbolPath,
        target_owner_qpath: SymbolPath,
        index: IndexQuery,
        is_declaring_file: bool,
        decl_byte_range: tuple[int, int] | None,
        new_name: str,
    ) -> RenameAnalysis:
        """Classify every attribute-style reference to `leaf` in this file.

        - `target_qpath`: full qpath of the symbol being renamed
          (e.g. "services.UserService.save").
        - `target_owner_qpath`: qpath of the declaring class
          (e.g. "services.UserService"). Adapter walks MRO against this.
        - `index`: queries the adapter needs (owners_of_leaf for the
          fast path, class_bases for MRO, find_declaration for resolved
          types).
        - `is_declaring_file` + `decl_byte_range`: when True, include
          the declaration's own identifier rewrite in the analysis.

        Each non-declaration `Attribute(attr==leaf)` site is bucketed:
        - rewrite       — receiver resolves to `target_owner_qpath` or a subclass
        - skipped_mismatch — receiver resolves to a different owner
        - unresolved    — receiver cannot be resolved statically
        """
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
