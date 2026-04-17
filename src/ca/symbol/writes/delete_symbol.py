"""`symbol delete-symbol` — remove a named symbol from its file.

Single-file write. Uses the symbol index to resolve name → byte range, then
composes symbol patch with empty content to do the actual splice. Refuses if
callers exist unless --force.

No read-cache check: delete-symbol's contract is identity-based, not byte-
based. The agent says "remove UserService.save"; we remove exactly that.
If the body changed since the agent last saw it, the symbol is still gone
— that's what the agent asked for.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ca.symbol.protocols import ReadCache
from ca.symbol.shared.symbol_index import SymbolIndex
from ca.symbol.shared.symbol import S_EBYTE, S_SBYTE
from ca.symbol.writes.patch import PatchRequest, apply_patch


@dataclass(frozen=True)
class CallerRef:
    file: str
    source_path: str | None  # containing symbol's qualified path, if any
    line: int
    kind: str                # "name" | "attr"


@dataclass(frozen=True)
class DeleteSymbolRequest:
    qualified_path: str
    kind: str
    file_abs: Path
    file_rel: str
    line_range: tuple[int, int]
    byte_range: tuple[int, int]
    callers: tuple[CallerRef, ...]
    force: bool


@dataclass(frozen=True)
class DeleteSymbolResult:
    status: Literal["applied", "dry_run", "error"]
    qualified_path: str = ""
    kind: str = ""
    file_rel: str = ""
    line_range: tuple[int, int] = (0, 0)
    callers: tuple[CallerRef, ...] = ()
    diff: str = ""
    lines_removed: int = 0
    error_code: str | None = None
    message: str | None = None
    # For ambiguous: list of candidate symbol paths.
    candidates: tuple[str, ...] = ()


def resolve_delete_symbol(
    index: SymbolIndex,
    qualified_path: str,
    project_root: Path,
    *,
    force: bool = False,
) -> DeleteSymbolRequest | DeleteSymbolResult:
    """Find the symbol + callers, enforce preconditions.

    Returns a DeleteSymbolRequest if the op can proceed (or is forced past
    live refs), or a DeleteSymbolResult with status='error' explaining
    why it can't.
    """
    if not index._built:
        index.build()

    rows = list(index.by_path.get(qualified_path, []))
    if not rows:
        return DeleteSymbolResult(
            status="error",
            error_code="symbol_not_found",
            message=f"no symbol at {qualified_path!r}",
        )

    if len(rows) > 1:
        return DeleteSymbolResult(
            status="error",
            error_code="symbol_ambiguous",
            message=f"{len(rows)} symbols match {qualified_path!r}",
            candidates=tuple(
                f"{index.file_of(r)}:{index.range_of(r)[0]}-{index.range_of(r)[1]}"
                for r in rows
            ),
        )

    row = rows[0]
    file_rel = index.file_of(row)
    file_abs = project_root / file_rel
    line_range = index.range_of(row)
    byte_range = (index.symbols[row][S_SBYTE], index.symbols[row][S_EBYTE])
    kind = index.kind_of(row)

    leaf = qualified_path.rsplit(".", 1)[-1]
    callers = tuple(
        CallerRef(
            file=index.file_of(src_row),
            source_path=index.path_of(src_row),
            line=line,
            kind=kind_label,
        )
        for src_row, line, kind_label in index.callers_of(leaf)
        if src_row != row
    )

    if callers and not force:
        return DeleteSymbolResult(
            status="error",
            error_code="has_live_references",
            message=f"{qualified_path} is referenced by {len(callers)} other symbol(s); use --force to delete anyway",
            qualified_path=qualified_path,
            kind=kind,
            file_rel=file_rel,
            line_range=line_range,
            callers=callers,
        )

    return DeleteSymbolRequest(
        qualified_path=qualified_path,
        kind=kind,
        file_abs=file_abs,
        file_rel=file_rel,
        line_range=line_range,
        byte_range=byte_range,
        callers=callers,
        force=force,
    )


def apply_delete_symbol(
    request: DeleteSymbolRequest,
    *,
    cache: ReadCache,
    dry_run: bool = False,
    diff_context: int = 5,
) -> DeleteSymbolResult:
    """Splice empty content over the symbol's byte range via the patch engine."""
    patch_req = PatchRequest(
        file_abs=request.file_abs,
        file_rel=request.file_rel,
        line_range=request.line_range,
        byte_range=request.byte_range,
        content=b"",
        force=True,  # delete-symbol bypasses patch's cache check by design
    )
    patch_result = apply_patch(
        patch_req, cache=cache, dry_run=dry_run, diff_context=diff_context
    )

    if patch_result.status == "error":
        return DeleteSymbolResult(
            status="error",
            qualified_path=request.qualified_path,
            kind=request.kind,
            file_rel=request.file_rel,
            line_range=request.line_range,
            callers=request.callers,
            error_code=patch_result.error_code,
            message=patch_result.message,
        )

    return DeleteSymbolResult(
        status=patch_result.status,  # "applied" or "dry_run"
        qualified_path=request.qualified_path,
        kind=request.kind,
        file_rel=request.file_rel,
        line_range=request.line_range,
        callers=request.callers,
        diff=patch_result.diff,
        lines_removed=patch_result.lines_removed,
    )
