"""`symbol replace-symbol` — replace a symbol's full definition, optionally renaming.

Flow:
1. Resolve the old symbol via the index.
2. Parse the new content via PythonAstAdapter (our own tier-1 adapter).
3. Validate: content parses, has exactly one top-level definition, same kind
   as the old symbol.
4. Extract the new leaf name from the parsed content.
5. If new name == old name → single-file patch (splice new content over old
   byte range).
6. If new name changed → multi-file op:
   - Splice new content over old byte range in the declaring file.
   - For every OTHER file that references the old leaf, regex rename
     old_leaf → new_name with word-boundary matching.
   - Collision check against siblings.
7. Commit atomically via writes/transaction.

Atomicity matters here: body change and ref updates land in one git
checkpoint, so there's never an intermediate state where the body is new
but refs are stale.
"""

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wyolet.symbol.adapters import default_registry
from wyolet.symbol.shared.symbol_index import SymbolIndex
from wyolet.symbol.shared.symbol import S_EBYTE, S_SBYTE
from wyolet.symbol.writes.transaction import FileEdit, commit_edits
from wyolet.symbol.writes._content import normalize_content
from wyolet.symbol.writes._blank_lines import normalize_file_blank_gaps


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


@dataclass(frozen=True)
class PerFileChange:
    file: str
    refs_updated: int


@dataclass(frozen=True)
class ReplaceSymbolRequest:
    qualified_path: str
    new_qualified_path: str
    old_leaf: str
    new_leaf: str
    name_changed: bool
    declaring_file: str
    edits: tuple[FileEdit, ...]
    per_file_counts: tuple[PerFileChange, ...]
    new_signature: str
    kind: str


@dataclass(frozen=True)
class ReplaceSymbolResult:
    status: Literal["applied", "dry_run", "error"]
    qualified_path: str = ""
    new_qualified_path: str = ""
    name_changed: bool = False
    declaring_file: str = ""
    new_signature: str = ""
    kind: str = ""
    files_changed: int = 0
    refs_updated: int = 0
    per_file: tuple[PerFileChange, ...] = ()
    error_code: str | None = None
    message: str | None = None
    candidates: tuple[str, ...] = ()


def resolve_replace_symbol(
    index: SymbolIndex,
    qualified_path: str,
    content: str,
    project_root: Path,
) -> ReplaceSymbolRequest | ReplaceSymbolResult:
    if not index._built:
        index.build()

    rows = list(index.by_path.get(qualified_path, []))
    if not rows:
        return ReplaceSymbolResult(
            status="error",
            error_code="symbol_not_found",
            message=f"no symbol at {qualified_path!r}",
        )
    if len(rows) > 1:
        return ReplaceSymbolResult(
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
    old_kind = index.kind_of(row)
    declaring_file_rel = index.file_of(row)
    declaring_file_abs = project_root / declaring_file_rel
    old_byte_range = (index.symbols[row][S_SBYTE], index.symbols[row][S_EBYTE])

    # Always normalize the agent's content to flush-left + re-indent to the
    # target's column, so they don't have to nail the exact leading whitespace.
    # Parse the dedented form so ast accepts it regardless of original indent.
    parse_bytes = textwrap.dedent(content).encode("utf-8")

    adapter = default_registry().for_file(declaring_file_abs)
    parse = adapter.validate_syntax(parse_bytes)
    if not parse.ok:
        return ReplaceSymbolResult(
            status="error",
            error_code="parse_broken",
            message=f"new content doesn't parse: line {parse.error_line}: {parse.error_message}",
        )

    # Extract top-level symbols from the new content.
    new_symbols = adapter.symbols(Path("<replace-content>"), parse_bytes)
    if len(new_symbols) == 0:
        return ReplaceSymbolResult(
            status="error",
            error_code="invalid_argument",
            message="new content contains no top-level symbol definitions",
        )
    if len(new_symbols) > 1:
        return ReplaceSymbolResult(
            status="error",
            error_code="invalid_argument",
            message=f"new content has {len(new_symbols)} top-level definitions; replace-symbol needs exactly one",
        )

    new_sym = new_symbols[0]
    new_leaf = new_sym.name
    new_kind = new_sym.kind

    if not _IDENT_RE.match(new_leaf):
        return ReplaceSymbolResult(
            status="error",
            error_code="invalid_argument",
            message=f"new symbol name {new_leaf!r} is not a valid identifier",
        )

    # Kind must match (can't replace a function with a class).
    if not _kinds_compatible(old_kind, new_kind):
        return ReplaceSymbolResult(
            status="error",
            error_code="invalid_argument",
            message=f"kind mismatch: old symbol is {old_kind!r}, new content defines a {new_kind!r}",
        )

    name_changed = new_leaf != old_leaf

    # Compute new qualified path.
    prefix = qualified_path[: -len(old_leaf)]  # keeps trailing "." or empty
    new_qpath = f"{prefix}{new_leaf}" if prefix else new_leaf

    if name_changed:
        # Collision check against siblings.
        if index.by_path.get(new_qpath):
            return ReplaceSymbolResult(
                status="error",
                error_code="name_collision",
                message=f"{new_qpath!r} already exists",
            )

    # Build the declaring-file edit: splice new content over old byte range.
    try:
        declaring_source = declaring_file_abs.read_bytes()
    except OSError as e:
        return ReplaceSymbolResult(
            status="error",
            error_code="file_not_found",
            message=f"cannot read {declaring_file_rel}: {e}",
        )

    start, end = old_byte_range
    if end > len(declaring_source):
        return ReplaceSymbolResult(
            status="error",
            error_code="conflict",
            message=f"declaring file shrank: byte range {start}-{end} exceeds current size",
        )

    # Build the declaring file's new content.
    # - The agent's new content goes in exactly as written.
    # - If name changed: any same-file callers (outside the replaced region)
    #   also need old_leaf → new_leaf. Rewrite before/after segments; don't
    #   touch the spliced-in region (it's already in the agent's chosen form).
    pattern = re.compile(rf"\b{re.escape(old_leaf)}\b") if name_changed else None
    before = declaring_source[:start]
    after = declaring_source[end:]
    declaring_refs_updated = 1  # the declaration itself
    if pattern is not None:
        before_text, c1 = pattern.subn(
            new_leaf, before.decode("utf-8", errors="replace")
        )
        after_text, c2 = pattern.subn(
            new_leaf, after.decode("utf-8", errors="replace")
        )
        before = before_text.encode("utf-8")
        after = after_text.encode("utf-8")
        declaring_refs_updated += c1 + c2

    target_indent = _indent_at_byte(declaring_source, start)
    content_bytes = normalize_content(content, target_indent)
    declaring_new_content = before + content_bytes + after

    edits: list[FileEdit] = [
        FileEdit(
            file_abs=declaring_file_abs,
            file_rel=declaring_file_rel,
            new_content=declaring_new_content,
        )
    ]

    per_file: list[PerFileChange] = [
        PerFileChange(file=declaring_file_rel, refs_updated=declaring_refs_updated)
    ]

    # If name changed: also rename old_leaf → new_leaf in every OTHER file that refs it.
    if name_changed:
        assert pattern is not None
        other_files: set[str] = set()
        for src_row, _line, _kind in index.callers_of(old_leaf):
            other_files.add(index.file_of(src_row))
        other_files.discard(declaring_file_rel)

        for rel in sorted(other_files):
            abs_path = project_root / rel
            try:
                src = abs_path.read_bytes()
            except OSError:
                continue
            text = src.decode("utf-8", errors="replace")
            new_text, count = pattern.subn(new_leaf, text)
            if count == 0:
                continue
            edits.append(
                FileEdit(
                    file_abs=abs_path,
                    file_rel=rel,
                    new_content=new_text.encode("utf-8"),
                )
            )
            per_file.append(PerFileChange(file=rel, refs_updated=count))

    # Extract new signature from the parsed (dedented) symbol.
    new_sig = _first_line(parse_bytes, new_sym.signature_line)

    return ReplaceSymbolRequest(
        qualified_path=qualified_path,
        new_qualified_path=new_qpath,
        old_leaf=old_leaf,
        new_leaf=new_leaf,
        name_changed=name_changed,
        declaring_file=declaring_file_rel,
        edits=tuple(edits),
        per_file_counts=tuple(per_file),
        new_signature=new_sig,
        kind=old_kind,
    )


def apply_replace_symbol(
    request: ReplaceSymbolRequest,
    *,
    project_root: Path,
    dry_run: bool = False,
) -> ReplaceSymbolResult:
    subject = (
        f"{request.qualified_path} (rewrite)"
        if not request.name_changed
        else f"{request.qualified_path} → {request.new_leaf} (rewrite + rename)"
    )
    tx = commit_edits(
        list(request.edits),
        project_root=project_root,
        op_name="replace-symbol",
        subject=subject,
        dry_run=dry_run,
    )

    if tx.status == "error":
        return ReplaceSymbolResult(
            status="error",
            qualified_path=request.qualified_path,
            new_qualified_path=request.new_qualified_path,
            name_changed=request.name_changed,
            declaring_file=request.declaring_file,
            new_signature=request.new_signature,
            kind=request.kind,
            error_code=tx.error_code,
            message=tx.message,
        )

    if not dry_run:
        declaring_abs = project_root / request.declaring_file
        normalize_file_blank_gaps(declaring_abs)

    total_refs = sum(f.refs_updated for f in request.per_file_counts)

    return ReplaceSymbolResult(
        status="dry_run" if dry_run else "applied",
        qualified_path=request.qualified_path,
        new_qualified_path=request.new_qualified_path,
        name_changed=request.name_changed,
        declaring_file=request.declaring_file,
        new_signature=request.new_signature,
        kind=request.kind,
        files_changed=len(request.edits),
        refs_updated=total_refs,
        per_file=request.per_file_counts,
    )


# ---------------------------------------------------------- helpers


def _kinds_compatible(old_kind: str, new_kind: str) -> bool:
    """Kinds in the same family match. `function` and `async_function` are
    one family; everything else must match exactly."""
    if old_kind == new_kind:
        return True
    families = [
        {"function", "async_function"},
        {"method", "async_method"},
    ]
    for fam in families:
        if old_kind in fam and new_kind in fam:
            return True
    return False


def _first_line(data: bytes, line_no: int) -> str:
    """Return the `line_no`-th line (1-indexed) of `data`, stripped."""
    lines = data.decode("utf-8", errors="replace").splitlines()
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1].strip()
    return ""
def _indent_at_byte(data: bytes, byte_pos: int) -> str:
    """Leading whitespace of the line containing `byte_pos`."""
    line_start = data.rfind(b"\n", 0, byte_pos) + 1
    out: list[str] = []
    i = line_start
    while i < len(data):
        ch = data[i:i + 1]
        if ch == b" " or ch == b"\t":
            out.append(ch.decode())
            i += 1
            continue
        break
    return "".join(out)
