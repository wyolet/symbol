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
