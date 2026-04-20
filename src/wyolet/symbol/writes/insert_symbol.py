"""`symbol insert-symbol` — add code at a position anchored to an existing symbol.

Resolves anchor qualified-path to a byte offset via the symbol index, then
calls the patch engine with a zero-width range and the new content.

Four positions:
- before: new lines go immediately above the anchor.
- after:  new lines go immediately below the anchor.
- start:  inside the anchor body, right after its signature.
- end:    inside the anchor body, just before its closing.

start/end are only valid for symbols with a body (class, function, method).

Indentation: by default we reindent the content to match the anchor's
scope — before/after use the anchor's own indent, start/end use one
indent step deeper. Agents can pass --no-reindent to send content as-is.
"""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wyolet.symbol.protocols import ReadCache
from wyolet.symbol.shared.symbol_index import SymbolIndex
from wyolet.symbol.shared.symbol import S_EBYTE, S_SBYTE
from wyolet.symbol.writes.patch import PatchRequest, apply_patch
from wyolet.symbol.writes._content import normalize_content
from wyolet.symbol.writes._blank_lines import normalize_file_blank_gaps


Position = Literal["before", "after", "start", "end"]


@dataclass(frozen=True)
class InsertSymbolRequest:
    anchor_path: str
    anchor_kind: str
    position: Position
    file_abs: Path
    file_rel: str
    # Line where the new content will land (1-indexed, inclusive).
    insert_line: int
    # Byte position for the zero-width splice.
    insert_byte: int
    content: bytes


@dataclass(frozen=True)
class InsertSymbolResult:
    status: Literal["applied", "dry_run", "error"]
    anchor_path: str = ""
    anchor_kind: str = ""
    position: str = ""
    file_rel: str = ""
    insert_line: int = 0
    diff: str = ""
    lines_added: int = 0
    error_code: str | None = None
    message: str | None = None
    candidates: tuple[str, ...] = ()


def resolve_insert_symbol(
    index: SymbolIndex,
    anchor_path: str,
    position: Position,
    content: str | bytes,
    project_root: Path,
    *,
    reindent: bool = True,
) -> InsertSymbolRequest | InsertSymbolResult:
    if not index._built:
        index.build()

    rows = list(index.by_path.get(anchor_path, []))
    if not rows:
        return InsertSymbolResult(
            status="error",
            error_code="symbol_not_found",
            message=f"no symbol at {anchor_path!r}",
        )
    if len(rows) > 1:
        return InsertSymbolResult(
            status="error",
            error_code="symbol_ambiguous",
            message=f"{len(rows)} symbols match {anchor_path!r}",
            candidates=tuple(
                f"{index.file_of(r)}:{index.range_of(r)[0]}-{index.range_of(r)[1]}"
                for r in rows
            ),
        )

    row = rows[0]
    file_rel = index.file_of(row)
    file_abs = project_root / file_rel
    start_line, end_line = index.range_of(row)
    start_byte, end_byte = index.symbols[row][S_SBYTE], index.symbols[row][S_EBYTE]
    kind = index.kind_of(row)

    has_body = kind in {"class", "function", "async_function", "method", "async_method"}
    if position in ("start", "end") and not has_body:
        return InsertSymbolResult(
            status="error",
            error_code="invalid_argument",
            message=f"position {position!r} requires a symbol with a body; {anchor_path} is {kind!r}",
        )

    # Read the file to compute exact byte positions and detect indent.
    try:
        source = file_abs.read_bytes()
    except OSError as e:
        return InsertSymbolResult(
            status="error",
            error_code="file_not_found",
            message=f"cannot read {file_rel}: {e}",
        )

    anchor_indent = _indent_of_line(source, start_line)
    body_indent = anchor_indent + "    "   # one step deeper

    if position == "before":
        # If anchor has decorators, insert above the first decorator — not
        # between decorator(s) and `def`, which would break the binding.
        # (start_line from the index is the def/class line.)
        anchor_top = _first_decorator_line(source, start_line) or start_line
        insert_line = anchor_top
        insert_byte = _line_start_byte(source, anchor_top)
        target_indent = anchor_indent
    elif position == "after":
        insert_line = end_line + 1
        insert_byte = _line_end_byte(source, end_line)
        target_indent = anchor_indent
    elif position == "start":
        # Right after the signature line (start_line has the def/class).
        insert_line = start_line + 1
        insert_byte = _line_end_byte(source, start_line)
        target_indent = body_indent
    elif position == "end":
        # Just before the anchor's closing — at the end of its last body line.
        insert_line = end_line + 1
        insert_byte = _line_end_byte(source, end_line)
        target_indent = body_indent
    else:
        return InsertSymbolResult(
            status="error",
            error_code="invalid_argument",
            message=f"position must be one of before/after/start/end, got {position!r}",
        )

    if reindent:
        payload = normalize_content(content, target_indent)
    else:
        payload = content.encode("utf-8") if isinstance(content, str) else content

    return InsertSymbolRequest(
        anchor_path=anchor_path,
        anchor_kind=kind,
        position=position,
        file_abs=file_abs,
        file_rel=file_rel,
        insert_line=insert_line,
        insert_byte=insert_byte,
        content=payload,
    )



def apply_insert_symbol(
    request: InsertSymbolRequest,
    *,
    cache: ReadCache,
    dry_run: bool = False,
    diff_context: int = 5,
) -> InsertSymbolResult:
    """Zero-width splice at request.insert_byte."""
    patch_req = PatchRequest(
        file_abs=request.file_abs,
        file_rel=request.file_rel,
        line_range=(request.insert_line, request.insert_line),
        byte_range=(request.insert_byte, request.insert_byte),
        content=request.content,
        force=True,
    )
    result = apply_patch(
        patch_req, cache=cache, dry_run=dry_run, diff_context=diff_context
    )
    if result.status == "applied":
        normalize_file_blank_gaps(request.file_abs)
        cache.invalidate(Path(request.file_rel))

    if result.status == "error":
        return InsertSymbolResult(
            status="error",
            anchor_path=request.anchor_path,
            anchor_kind=request.anchor_kind,
            position=request.position,
            file_rel=request.file_rel,
            insert_line=request.insert_line,
            error_code=result.error_code,
            message=result.message,
        )

    return InsertSymbolResult(
        status=result.status,
        anchor_path=request.anchor_path,
        anchor_kind=request.anchor_kind,
        position=request.position,
        file_rel=request.file_rel,
        insert_line=request.insert_line,
        diff=result.diff,
        lines_added=result.lines_added,
    )


# ---------------------------------------------------------- helpers


def _line_start_byte(data: bytes, line: int) -> int:
    if line <= 1:
        return 0
    seen = 1
    for i, b in enumerate(data):
        if b == 0x0A:
            seen += 1
            if seen == line:
                return i + 1
    return len(data)


def _line_end_byte(data: bytes, line: int) -> int:
    """Byte just past the newline of `line`."""
    seen = 0
    for i, b in enumerate(data):
        if b == 0x0A:
            seen += 1
            if seen == line:
                return i + 1
    return len(data)
def _first_decorator_line(source: bytes, def_line: int) -> int | None:
    """Line number of the topmost decorator on the def/class at `def_line`.

    Returns None when the symbol has no decorators or when source can't be
    parsed — callers fall back to `def_line`. Uses ast (not textual walk) so
    `@` characters inside string literals or inside decorator argument
    expressions don't fool us.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.lineno == def_line and node.decorator_list:
                return min(d.lineno for d in node.decorator_list)
    return None


def _indent_of_line(data: bytes, line: int) -> str:
    """Leading whitespace of `line` (spaces only; tabs preserved as-is)."""
    start = _line_start_byte(data, line)
    i = start
    while i < len(data) and data[i] in (0x20, 0x09):  # space or tab
        i += 1
    return data[start:i].decode("utf-8", errors="replace")

