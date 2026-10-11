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


_FUNC_FAMILY = frozenset({"function", "async_function", "method", "async_method"})


@dataclass(frozen=True)
class PerFileChange:
    file: str
    refs_updated: int


@dataclass(frozen=True)
class ParameterRename:
    """A parameter rename detected from a signature edit and propagated to
    keyword-argument call sites. `refs_updated`/`skipped_mismatch`/`unresolved`
    are aggregate site counts across all candidate files for this one param."""
    old: str
    new: str
    refs_updated: int
    skipped_mismatch: int
    unresolved: int


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
    parameter_renames: tuple[ParameterRename, ...] = ()
    signature_changed: bool = False
    old_parameters: tuple[str, ...] = ()
    new_parameters: tuple[str, ...] = ()


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
    parameter_renames: tuple[ParameterRename, ...] = ()
    signature_changed: bool = False
    old_parameters: tuple[str, ...] = ()
    new_parameters: tuple[str, ...] = ()
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
    declaring_file_rel = index.file_of(row)
    # Guard against stale byte ranges: if the file's been edited since the
    # index recorded it, refresh and re-resolve before computing the splice.
    if index.ensure_fresh(declaring_file_rel):
        rows = list(index.by_path.get(qualified_path, []))
        if not rows:
            return ReplaceSymbolResult(
                status="error",
                error_code="symbol_not_found",
                message=f"symbol {qualified_path!r} no longer exists after refresh",
            )
        row = rows[0]
        declaring_file_rel = index.file_of(row)

    old_leaf = qualified_path.rsplit(".", 1)[-1]
    old_kind = index.kind_of(row)
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

    # Parameter renames are a CONSEQUENCE of a signature edit, not a verb.
    # When the target is function-family, compare the OLD def's parameter list
    # against the NEW content's. A conservative positional pairing detects
    # unambiguous renames and propagates them to keyword-argument call sites
    # (folded into the SAME transaction). Arity change / swap / reorder →
    # no propagation, just `signature_changed` for the caller's awareness.
    parameter_renames: tuple[ParameterRename, ...] = ()
    signature_changed = False
    old_params_out: tuple[str, ...] = ()
    new_params_out: tuple[str, ...] = ()

    if old_kind in _FUNC_FAMILY and hasattr(adapter, "function_parameters") and hasattr(adapter, "rename_parameter"):
        mp = adapter.module_prefix(declaring_file_abs, project_root)
        old_slice = textwrap.dedent(
            declaring_source[start:end].decode("utf-8", errors="replace")
        ).encode("utf-8")
        old_params = adapter.function_parameters(
            path=declaring_file_abs, project_root=project_root, source=old_slice,
            func_qpath=f"{mp}.{old_leaf}" if mp else old_leaf, decl_byte_range=None,
        )
        new_params = adapter.function_parameters(
            path=declaring_file_abs, project_root=project_root, source=parse_bytes,
            func_qpath=f"{mp}.{new_leaf}" if mp else new_leaf, decl_byte_range=None,
        )
        detected = _detect_param_renames(old_params, new_params)
        if detected is None:
            # arity change or ambiguous pairing (swap/reorder) — decline to
            # propagate, but tell the caller the signature moved so they know
            # kwarg sites were NOT touched.
            if (
                old_params is not None
                and new_params is not None
                and tuple(old_params) != tuple(new_params)
            ):
                signature_changed = True
                old_params_out = tuple(old_params)
                new_params_out = tuple(new_params)
        elif detected:
            edits, parameter_renames = _propagate_param_renames(
                adapter=adapter, index=index, project_root=project_root,
                declaring_file_rel=declaring_file_rel, func_leaf=old_leaf,
                new_leaf=new_leaf, new_qpath=new_qpath, renames=detected,
                edits=edits,
            )

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
        parameter_renames=parameter_renames,
        signature_changed=signature_changed,
        old_parameters=old_params_out,
        new_parameters=new_params_out,
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
        parameter_renames=request.parameter_renames,
        signature_changed=request.signature_changed,
        old_parameters=request.old_parameters,
        new_parameters=request.new_parameters,
    )


# ---------------------------------------------------------- parameter renames


def _detect_param_renames(
    old_params: tuple[str, ...] | None,
    new_params: tuple[str, ...] | None,
) -> list[tuple[str, str]] | None:
    """Conservative positional pairing of old→new parameter lists.

    Returns the list of `(old, new)` renames, or None if the edit is NOT a
    clean set of renames (arity changed, or a swap/reorder where a "new" name
    already existed in the old list — or vice versa). An empty list means the
    parameter lists are identical (a pure body edit).
    """
    if old_params is None or new_params is None:
        return None
    if len(old_params) != len(new_params):
        return None  # arity changed → not a rename
    old_set = set(old_params)
    new_set = set(new_params)
    renames: list[tuple[str, str]] = []
    for old, new in zip(old_params, new_params):
        if old == new:
            continue
        # Unambiguous only when the new name is genuinely fresh (never in the
        # old list) AND the old name is genuinely gone (never in the new list).
        # This rules out swaps (a,b)->(b,a) and reorders.
        if new in old_set or old in new_set:
            return None
        renames.append((old, new))
    return renames


def _apply_byte_rewrites(source: bytes, rewrites) -> bytes:
    buf = bytearray(source)
    for r in sorted(rewrites, key=lambda x: x.byte_start, reverse=True):
        buf[r.byte_start:r.byte_end] = r.new_text.encode("utf-8")
    return bytes(buf)


def _propagate_param_renames(
    *,
    adapter,
    index: SymbolIndex,
    project_root: Path,
    declaring_file_rel: str,
    func_leaf: str,
    new_leaf: str,
    new_qpath: str,
    renames: list[tuple[str, str]],
    edits: list[FileEdit],
) -> tuple[list[FileEdit], tuple[ParameterRename, ...]]:
    """Run the kwargs_only parameter-rename engine per candidate file and fold
    the resulting keyword-argument rewrites into the existing FileEdit set.

    The analysis runs against POST-splice content: the declaring file's edit
    already carries the new signature/body, and any function-name-rename edits
    on other files are already applied — so callee resolution keys off the
    POST-rename identity (`new_leaf` / `new_qpath`). Rewrites for each param
    are applied sequentially per file (content recomputed between params).
    """
    from wyolet.symbol.writes.rename.index_query import RenamerIndexQuery

    iq = RenamerIndexQuery(index, project_root)

    # Pending content, seeded from the edits already staged (declaring splice +
    # cross-file function-name renames). Files not yet edited are read on demand.
    pending: dict[str, bytes] = {e.file_rel: e.new_content for e in edits}
    order: list[str] = [e.file_rel for e in edits]

    # Candidate files (reuses the renamer enumeration): declaring file +
    # in-body callers of the leaf + module-level import-alias files.
    candidates: set[str] = {declaring_file_rel}
    for src_row, _line, _kind in index.callers_of(func_leaf):
        candidates.add(index.file_of(src_row))
    for file_id in range(len(index.files)):
        for local, _source, _line in index.imports_for(file_id):
            if local == func_leaf:
                candidates.add(index.files[file_id])
                break

    agg: dict[tuple[str, str], list[int]] = {(o, n): [0, 0, 0] for o, n in renames}

    for rel in sorted(candidates):
        if index.language_of_file(rel) != "python":
            continue
        abs_path = project_root / rel
        if rel in pending:
            current = pending[rel]
        else:
            try:
                current = abs_path.read_bytes()
            except OSError:
                continue
        changed = False
        for old, new in renames:
            analysis = adapter.rename_parameter(
                path=abs_path, project_root=project_root, source=current,
                func_qpath=new_qpath, func_leaf=new_leaf, param=old,
                index=iq, is_declaring_file=(rel == declaring_file_rel),
                decl_byte_range=None, new_name=new, kwargs_only=True,
            )
            counts = agg[(old, new)]
            counts[0] += len(analysis.rewrites)
            counts[1] += len(analysis.skipped_mismatch)
            counts[2] += len(analysis.unresolved)
            if analysis.rewrites:
                current = _apply_byte_rewrites(current, analysis.rewrites)
                changed = True
        if changed:
            pending[rel] = current
            if rel not in order:
                order.append(rel)

    new_edits = [
        FileEdit(file_abs=project_root / rel, file_rel=rel, new_content=pending[rel])
        for rel in order
    ]
    parameter_renames = tuple(
        ParameterRename(
            old=o, new=n,
            refs_updated=c[0], skipped_mismatch=c[1], unresolved=c[2],
        )
        for (o, n), c in agg.items()
    )
    return new_edits, parameter_renames


# ---------------------------------------------------------- helpers


def _kinds_compatible(old_kind: str, new_kind: str) -> bool:
    """Kinds in the same family match. All callable kinds — `function`,
    `async_function`, `method`, `async_method` — form ONE family: a method
    replacement is authored as a bare top-level ``def`` (there is no other way
    to write a single-symbol body), so the new content scans as a `function`
    even when the target is a `method`. Non-callable kinds must match exactly."""
    if old_kind == new_kind:
        return True
    callable_kinds = {"function", "async_function", "method", "async_method"}
    return old_kind in callable_kinds and new_kind in callable_kinds


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
