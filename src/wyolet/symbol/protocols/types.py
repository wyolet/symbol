"""Return types for language adapter protocols.

All plain frozen dataclasses: simple, hashable, serializable. Adapters emit
these; pipeline and command resolvers consume them without ever touching
adapter-internal tree types (ast.Module, tree_sitter.Node, etc.).
"""

from dataclasses import dataclass, field

# Language-native qualified path (e.g. "services.user.UserService.save").
# Format is per-language; we don't normalize across languages.
SymbolPath = str


@dataclass(frozen=True)
class RawSymbol:
    """A single declared symbol: class, function, method, constant, etc.

    Kind strings are per-language ("class", "function", "async_function",
    "method", "struct", ...). No normalization across languages.

    Byte range end is exclusive. Line range ends are inclusive, 1-indexed.
    Children carry nested symbols (a class's methods) in the natural tree
    shape. The index layer flattens them for columnar storage.
    """

    kind: str
    name: str
    qualified_path: SymbolPath
    byte_range: tuple[int, int]
    line_range: tuple[int, int]
    signature_line: int
    children: tuple["RawSymbol", ...] = ()


@dataclass(frozen=True)
class RawImport:
    """An import statement in a file.

    `statement` is the raw text of the import line(s); useful when the agent
    needs to replicate it elsewhere (e.g. move-symbol suggesting imports for
    the destination file).
    """

    line: int
    byte_range: tuple[int, int]
    statement: str
    imported_names: tuple[str, ...]
    module: str | None


@dataclass(frozen=True)
class RawRef:
    """A name reference inside a symbol's body.

    Textual level: we know the name occurs here, but binding resolution (what
    symbol does this name refer to) is the job of SemanticLanguageAdapter.
    """

    name: str
    line: int
    byte_offset: int


@dataclass(frozen=True)
class ScannedRef:
    """A name reference inside a symbol's body, with ref kind.

    kind ∈ {"name", "attr"}. Attribute refs capture the dotted tail
    (``foo`` in ``x.foo``) — essential for tier-1 callers lookup of
    method-style calls.
    """

    name: str
    kind: str
    line: int


@dataclass(frozen=True)
class ScannedSymbol:
    """A declared symbol plus the refs from its direct scope.

    Drives the symbol index. Richer than RawSymbol: each scanned symbol
    carries its own ref list so the builder doesn't have to recurse back
    into the adapter for every row.
    """

    kind: str
    name: str
    qualified_path: SymbolPath
    byte_range: tuple[int, int]
    line_range: tuple[int, int]
    refs: tuple[ScannedRef, ...] = ()
    children: tuple["ScannedSymbol", ...] = ()


@dataclass(frozen=True)
class ScannedImport:
    """One import binding in a file. Per-alias, not per-statement.

    ``local`` is the name visible in the importing file (alias or first
    segment). ``source`` is the module it came from — for ``import a.b``
    it's ``"a.b"``; for ``from x import y`` it's ``"x"``.
    """

    local: str
    source: str
    line: int


@dataclass(frozen=True)
class FileScan:
    """Full scan of one file produced by ``LanguageAdapter.scan_file``.

    ``ok=False`` with an ``error`` means the file couldn't be parsed;
    the builder treats it as a skipped file (no symbols, no imports).
    """

    language: str
    imports: tuple[ScannedImport, ...] = ()
    symbols: tuple[ScannedSymbol, ...] = ()
    ok: bool = True
    error: str | None = None


@dataclass(frozen=True)
class ParseResult:
    """Result of a syntax validation check on a byte blob."""

    ok: bool
    error_line: int | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BindingResolution:
    """Result of resolving a name at a location to the symbol it binds to.

    `unsupported=True` signals that the adapter cannot handle this specific
    case (e.g. local scope resolution when the adapter only does module-scope
    binding). The pipeline surfaces `reason` in user-facing errors.
    """

    ok: bool
    symbol: SymbolPath | None = None
    reason: str | None = None
    unsupported: bool = False


@dataclass(frozen=True)
class ReferenceResult:
    """Result of a reverse reference lookup for a symbol."""

    ok: bool
    refs: tuple[RawRef, ...] = ()
    reason: str | None = None
    unsupported: bool = False


@dataclass(frozen=True)
class ByteRewrite:
    """A single byte-range edit produced by an adapter for the renamer.

    Byte range is the exact identifier token — never wraps punctuation,
    never falls inside a string/comment, never spans a different name.
    `line`/`col` are informational (for telemetry and result surfaces).
    `receiver_source` is the source text of an attribute receiver
    (`"self"`, `"b"`, `"Foo.bar"`) or empty for non-attribute rewrites.
    """

    byte_start: int
    byte_end: int
    new_text: str
    line: int
    col: int
    receiver_source: str = ""


@dataclass(frozen=True)
class SkippedMismatchSite:
    """An identifier with the right leaf name that resolved to a *different*
    declaration — correctly skipped by the discriminator."""

    byte_start: int
    byte_end: int
    line: int
    col: int
    receiver_source: str
    resolved_to_qpath: SymbolPath


@dataclass(frozen=True)
class UnresolvedSite:
    """An identifier with the right leaf name whose binding the adapter
    could not resolve. Aborts the rename unless `force=True`."""

    byte_start: int
    byte_end: int
    line: int
    col: int
    receiver_source: str
    why: str


@dataclass(frozen=True)
class RenameAnalysis:
    """One file's classification produced by `adapter.rename_*` methods.

    Adapter owns the algorithm; the neutral renamer applies the uniform
    policy (apply rewrites, surface unresolved). Empty tuples mean
    nothing of that kind was found in the file.
    """

    rewrites: tuple[ByteRewrite, ...] = ()
    skipped_mismatch: tuple[SkippedMismatchSite, ...] = ()
    unresolved: tuple[UnresolvedSite, ...] = ()
