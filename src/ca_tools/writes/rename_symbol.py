"""`ca rename-symbol` — rename a symbol and its tier-1 textual references.

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

from ca_tools.shared.symbol_index import SymbolIndex
from ca_tools.shared.symbol import S_EBYTE, S_SBYTE
from ca_tools.writes.transaction import FileEdit, TransactionResult, commit_edits


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
    edits: tuple[FileEdit, ...]
    per_file_counts: tuple[FileRename, ...]


@dataclass(frozen=True)
class RenameSymbolResult:
    status: Literal["applied", "dry_run", "error"]
    qualified_path: str = ""
    new_qualified_path: str = ""
    files_changed: int = 0
    refs_updated: int = 0
    per_file: tuple[FileRename, ...] = ()
    declaring_file: str = ""
    error_code: str | None = None
    message: str | None = None
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
                f"Use `ca move-symbol` to change the module path."
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

    declaring_file_rel = index.file_of(row)

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


def apply_rename_symbol(
    request: RenameSymbolRequest,
    *,
    project_root: Path,
    dry_run: bool = False,
    allow_dirty: bool = False,
    force_no_vcs: bool = False,
) -> RenameSymbolResult:
    subject = f"{request.qualified_path} → {request.new_name}"
    tx = commit_edits(
        list(request.edits),
        project_root=project_root,
        op_name="rename-symbol",
        subject=subject,
        allow_dirty=allow_dirty,
        force_no_vcs=force_no_vcs,
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
