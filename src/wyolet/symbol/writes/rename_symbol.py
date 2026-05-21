"""`symbol rename-symbol` — rename a symbol and its tier-1 textual references.

Flow:
1. Resolve declaration via the symbol index.
2. Validate new-name is a bare identifier.
3. Check for name collisions at the declaring scope.
4. Find files containing the old leaf name (via the index's ref table +
   the declaring file).
5. Per file, run a word-boundary regex replace old_leaf → new_name.
6. Build multi-file FileEdit list.
7. Commit atomically via writes/transaction.
8. Report file counts, ref counts, unresolved (comment/string) occurrences.

Tier-1 textual. Scope-blind. Same-name shadowing is not detected. Agent
is told which files changed and how many refs per file so it can verify
if it wants.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wyolet.symbol.shared.symbol_index import SymbolIndex
from wyolet.symbol.shared.symbol import S_EBYTE, S_SBYTE
from wyolet.symbol.writes.transaction import FileEdit, TransactionResult, commit_edits


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


@dataclass(frozen=True)
class FileRename:
    file: str
    refs_updated: int


@dataclass(frozen=True)
class RenameSymbolRequest:
    qualified_path: str
    old_leaf: str
    new_name: str
    new_qualified_path: str
    declaring_file: str
    edits: tuple[FileEdit, ...] = ()
    per_file_counts: tuple[FileRename, ...] = ()
    # v2 engine routing — when True, the request carries no precomputed
    # edits; apply_rename_symbol delegates to SymbolRenamer instead.
    via_engine: bool = False


@dataclass(frozen=True)
class _UnresolvedSitePub:
    file: str
    line: int
    col: int
    receiver_source: str
    why: str


@dataclass(frozen=True)
class _SkippedSitePub:
    file: str
    line: int
    col: int
    receiver_source: str
    resolved_to_qpath: str


@dataclass(frozen=True)
class _AffectedInterfacePub:
    interface_qpath: str
    method_qpath: str
    file: str
    line: int


@dataclass(frozen=True)
class RenameSymbolResult:
    status: Literal["applied", "dry_run", "needs_review", "error"]
    qualified_path: str = ""
    new_qualified_path: str = ""
    files_changed: int = 0
    refs_updated: int = 0
    per_file: tuple[FileRename, ...] = ()
    declaring_file: str = ""
    error_code: str | None = None
    message: str | None = None
    # v2 engine output (empty on tier-1 textual path)
    unresolved: tuple[_UnresolvedSitePub, ...] = ()
    skipped_mismatch: tuple[_SkippedSitePub, ...] = ()
    affected_interfaces: tuple[_AffectedInterfacePub, ...] = ()
    candidates: tuple[str, ...] = ()


def resolve_rename_symbol(
    index: SymbolIndex,
    qualified_path: str,
    new_name: str,
    project_root: Path,
) -> RenameSymbolRequest | RenameSymbolResult:
    if not index._built:
        index.build()

    if not _IDENT_RE.match(new_name):
        return RenameSymbolResult(
            status="error",
            error_code="invalid_argument",
            message=(
                f"new-name must be a bare identifier (letters, digits, underscores). "
                f"Use `symbol move-symbol` to change the module path."
            ),
        )

    rows = list(index.by_path.get(qualified_path, []))
    if not rows:
        return RenameSymbolResult(
            status="error",
            error_code="symbol_not_found",
            message=f"no symbol at {qualified_path!r}",
        )
    if len(rows) > 1:
        return RenameSymbolResult(
            status="error",
            error_code="symbol_ambiguous",
            message=f"{len(rows)} symbols match {qualified_path!r}",
            candidates=tuple(
                f"{index.file_of(r)}:{index.range_of(r)[0]}-{index.range_of(r)[1]}"
                for r in rows
            ),
        )

    row = rows[0]
    declaring_file_rel = index.file_of(row)
    if index.ensure_fresh(declaring_file_rel):
        rows = list(index.by_path.get(qualified_path, []))
        if not rows:
            return RenameSymbolResult(
                status="error",
                error_code="symbol_not_found",
                message=f"symbol {qualified_path!r} no longer exists after refresh",
            )
        row = rows[0]
        declaring_file_rel = index.file_of(row)

    old_leaf = qualified_path.rsplit(".", 1)[-1]

    if old_leaf == new_name:
        return RenameSymbolResult(
            status="error",
            error_code="invalid_argument",
            message=f"new-name is identical to current name",
        )

    # Collision check: does new_name exist as a sibling at the same scope?
    prefix = qualified_path[: -(len(old_leaf))]  # keeps trailing "." or empty
    collision_path = f"{prefix}{new_name}" if prefix else new_name
    if index.by_path.get(collision_path):
        return RenameSymbolResult(
            status="error",
            error_code="name_collision",
            message=f"{collision_path!r} already exists",
        )

    # v2 engine routing: methods, functions, classes (and Go equivalents)
    # go through SymbolRenamer. Python uses an in-process AST resolver;
    # Go delegates to the daemon which uses go/types for semantic-correct
    # receiver resolution. Other kinds fall through to the tier-1 regex
    # path below.
    kind = index.kind_of(row)
    declaring_lang = index.language_of_file(declaring_file_rel)
    if (
        declaring_lang in ("python", "go")
        and kind in (
            # python
            "method", "async_method", "function", "async_function", "class", "constant",
            # go (per adapters/go_ast scan output)
            "type", "var", "const",
        )
    ):
        new_qpath = f"{prefix}{new_name}" if prefix else new_name
        return RenameSymbolRequest(
            qualified_path=qualified_path,
            old_leaf=old_leaf,
            new_name=new_name,
            new_qualified_path=new_qpath,
            declaring_file=declaring_file_rel,
            via_engine=True,
        )

    # Collect every file that references the old leaf, plus the declaring file.
    ref_files: set[str] = {declaring_file_rel}
    for src_row, _line, _kind in index.callers_of(old_leaf):
        ref_files.add(index.file_of(src_row))

    # Per-file textual rewrite with word boundaries.
    pattern = re.compile(rf"\b{re.escape(old_leaf)}\b")
    edits: list[FileEdit] = []
    per_file: list[FileRename] = []

    for rel in sorted(ref_files):
        abs_path = project_root / rel
        try:
            source = abs_path.read_bytes()
        except OSError:
            continue
        text = source.decode("utf-8", errors="replace")
        new_text, count = pattern.subn(new_name, text)
        if count == 0:
            continue
        edits.append(
            FileEdit(
                file_abs=abs_path,
                file_rel=rel,
                new_content=new_text.encode("utf-8"),
            )
        )
        per_file.append(FileRename(file=rel, refs_updated=count))

    if not edits:
        return RenameSymbolResult(
            status="error",
            error_code="nothing_to_rename",
            message=f"no occurrences of {old_leaf!r} found (even in the declaring file — this is unusual)",
        )

    new_qpath = f"{prefix}{new_name}" if prefix else new_name

    return RenameSymbolRequest(
        qualified_path=qualified_path,
        old_leaf=old_leaf,
        new_name=new_name,
        new_qualified_path=new_qpath,
        declaring_file=declaring_file_rel,
        edits=tuple(edits),
        per_file_counts=tuple(per_file),
    )


def _apply_via_engine(
    request: RenameSymbolRequest,
    *,
    project_root: Path,
    dry_run: bool,
    index: SymbolIndex | None,
) -> RenameSymbolResult:
    from wyolet.symbol.adapters.registry import default_registry
    from wyolet.symbol.writes.rename import SymbolRenamer

    if index is None:
        from wyolet.symbol.shared.symbol_index import get_or_build_index
        index, _ = get_or_build_index(project_root)

    renamer = SymbolRenamer(index, project_root, default_registry())
    r = renamer.rename(request.qualified_path, request.new_name, dry_run=dry_run)

    return RenameSymbolResult(
        status=r.status,
        qualified_path=r.qualified_path,
        new_qualified_path=r.new_qualified_path,
        files_changed=r.files_changed,
        refs_updated=r.refs_updated,
        per_file=tuple(FileRename(file=f.file, refs_updated=f.refs_updated) for f in r.per_file),
        declaring_file=r.declaring_file,
        error_code=r.error_code,
        message=r.message,
        unresolved=tuple(
            _UnresolvedSitePub(
                file=u.file, line=u.line, col=u.col,
                receiver_source=u.receiver_source, why=u.why,
            ) for u in r.unresolved
        ),
        skipped_mismatch=tuple(
            _SkippedSitePub(
                file=s.file, line=s.line, col=s.col,
                receiver_source=s.receiver_source,
                resolved_to_qpath=s.resolved_to_qpath,
            ) for s in r.skipped_mismatch
        ),
        affected_interfaces=tuple(
            _AffectedInterfacePub(
                interface_qpath=a.interface_qpath,
                method_qpath=a.method_qpath,
                file=a.file,
                line=a.line,
            ) for a in r.affected_interfaces
        ),
    )


def apply_rename_symbol(
    request: RenameSymbolRequest,
    *,
    project_root: Path,
    dry_run: bool = False,
    _index: SymbolIndex | None = None,
) -> RenameSymbolResult:
    if request.via_engine:
        return _apply_via_engine(request, project_root=project_root, dry_run=dry_run, index=_index)

    subject = f"{request.qualified_path} → {request.new_name}"
    tx = commit_edits(
        list(request.edits),
        project_root=project_root,
        op_name="rename-symbol",
        subject=subject,
        dry_run=dry_run,
    )

    if tx.status == "error":
        return RenameSymbolResult(
            status="error",
            qualified_path=request.qualified_path,
            new_qualified_path=request.new_qualified_path,
            declaring_file=request.declaring_file,
            error_code=tx.error_code,
            message=tx.message,
        )

    total_refs = sum(f.refs_updated for f in request.per_file_counts)

    return RenameSymbolResult(
        status="dry_run" if dry_run else "applied",
        qualified_path=request.qualified_path,
        new_qualified_path=request.new_qualified_path,
        declaring_file=request.declaring_file,
        files_changed=len(request.edits),
        refs_updated=total_refs,
        per_file=request.per_file_counts,
    )
