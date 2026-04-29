"""MCP server entry point.

Single-root v1: one server = one project. Builds a Workspace + warm
SymbolIndex + InMemoryReadCache once at startup; every tool call reuses
them. Multi-root lifespan-scoped sessions are a follow-up.
"""

from dataclasses import asdict, is_dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from wyolet.symbol.adapters.registry import default_registry
from wyolet.symbol.caches import InMemoryReadCache, record_served
from wyolet.symbol.mcp import descriptions
from wyolet.symbol.reads.callers import callers as callers_read
from wyolet.symbol.reads.code import CodeAmbiguous, CodeNotFound, code as code_read
from wyolet.symbol.reads.outline import outline as outline_read
from wyolet.symbol.reads.search import search as search_read
from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.shared.workspace import build_workspace
from wyolet.symbol.writes.delete_symbol import (
    DeleteSymbolRequest,
    apply_delete_symbol,
    resolve_delete_symbol,
)
from wyolet.symbol.writes.insert_symbol import (
    InsertSymbolRequest,
    apply_insert_symbol,
    resolve_insert_symbol,
)
from wyolet.symbol.writes.patch import (
    PatchPreflight,
    apply_patch,
    apply_patch_multi,
    preflight_patch,
    validate_args,
)
from wyolet.symbol.writes.undo import undo_last as _undo_last
from wyolet.symbol.writes.rename_symbol import (
    RenameSymbolRequest,
    apply_rename_symbol,
    resolve_rename_symbol,
)
from wyolet.symbol.writes.replace_symbol import (
    ReplaceSymbolRequest,
    apply_replace_symbol,
    resolve_replace_symbol,
)

import contextlib
import io

from wyolet.symbol.commands.delete_symbol import (
    _render_agent as _render_delete_agent,
    _render_error as _render_delete_error,
)
from wyolet.symbol.commands.insert_symbol import _render as _render_insert
from wyolet.symbol.commands.patch import (
    _render_error as _render_patch_error,
    _render_needs_confirmation_agent as _render_patch_needs_read,
    _render_result_agent as _render_patch_agent,
)
from wyolet.symbol.commands.rename_symbol import (
    _render_agent as _render_rename_agent,
    _render_error as _render_rename_error,
)
from wyolet.symbol.commands.replace_symbol import (
    _render_agent as _render_replace_agent,
    _render_error as _render_replace_error,
)

mcp = FastMCP("symbol")


def _capture_text(fn, *args, **kwargs) -> str:
    """Run a print-based renderer and return what it printed.

    The CLI agent renderers (`_render_*_agent`, `_render_error(...,agent=True)`)
    print to stdout. MCP needs them as a string so we can return that to the
    agent. This is the cheapest way to share the same render layer across
    CLI (--agent flag) and MCP (always agent format).
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue().rstrip("\n")


class _State:
    """Process-wide state. Set once by ``serve``, read by tool handlers.

    Holds the SymbolIndex in RAM for the life of the MCP server — long-lived
    stdio process means we should not re-load 75KB-many-MB pickle on every
    tool call. Refreshes from disk only when the on-disk file's mtime is newer
    than what we cached (e.g. after a PostToolUse hook refresh from another
    process).
    """

    project_root: Path | None = None
    read_cache: InMemoryReadCache | None = None
    _index: "SymbolIndex | None" = None
    _index_mtime: float | None = None

    @classmethod
    def initialize(cls, project_root: Path) -> None:
        cls.project_root = project_root.resolve()
        cls.read_cache = InMemoryReadCache()
        build_workspace(cls.project_root)
        cls._load_index()

    @classmethod
    def _index_path(cls) -> Path:
        return cls.require_root() / ".symbol" / "symbol_index.msgpack.zst"

    @classmethod
    def _load_index(cls) -> None:
        idx, source = get_or_build_index(cls.require_root())
        cls._index = idx
        p = cls._index_path()
        cls._index_mtime = p.stat().st_mtime if p.exists() else None
        cls._index_source = source

    @classmethod
    def require_root(cls) -> Path:
        if cls.project_root is None:
            raise RuntimeError("MCP server not initialized — call serve() first")
        return cls.project_root

    @classmethod
    def require_cache(cls) -> InMemoryReadCache:
        if cls.read_cache is None:
            raise RuntimeError("MCP server not initialized — call serve() first")
        return cls.read_cache

    @classmethod
    def require_index(cls) -> "SymbolIndex":
        """Return the in-RAM index. Reload from disk only if disk is newer."""
        if cls._index is None:
            cls._load_index()
        else:
            p = cls._index_path()
            if p.exists():
                disk_mtime = p.stat().st_mtime
                if cls._index_mtime is None or disk_mtime > cls._index_mtime:
                    cls._load_index()
        assert cls._index is not None
        return cls._index

    @classmethod
    def ensure_indexed(cls, file: str) -> tuple[str, str | None]:
        """Make sure `file` is in the index, indexing on the fly if needed.

        `file` is a project-relative or absolute path (no `:line` suffix —
        callers must split that off first). Returns (status, file_rel):

        - "ok": already indexed, or just got indexed.
        - "not_found": file doesn't exist on disk.
        - "outside_project": resolves outside project_root.
        - "unsupported": exists but no registered adapter handles it (md,
          json, binary). file_rel still populated for error messages.

        Single-file refresh is cheap: one read + AST parse, no project scan.
        Linguist content-sniffs files with ambiguous or missing extensions.
        """
        root = cls.require_root()
        p = Path(file)
        abs_path = (p if p.is_absolute() else root / p).resolve()
        try:
            file_rel = str(abs_path.relative_to(root))
        except ValueError:
            return ("outside_project", None)

        if not abs_path.is_file():
            return ("not_found", file_rel)

        index = cls.require_index()
        if index.language_of_file(file_rel) is not None:
            return ("ok", file_rel)

        if not default_registry().supports(abs_path):
            return ("unsupported", file_rel)

        index.refresh(stale={file_rel})
        index.save()
        ipath = cls._index_path()
        cls._index_mtime = ipath.stat().st_mtime if ipath.exists() else cls._index_mtime
        return ("ok", file_rel)

    @classmethod
    def invalidate_file(cls, rel: "str | list[str] | set[str]") -> None:
        """Refresh the in-RAM index for one or more files immediately.

        MCP write tools must call this after a successful write so the next tool
        call sees updated byte/line offsets. Without it, the in-RAM cache stays
        pinned to pre-write state until the disk-mtime check trips on a later
        save (which is racy with the PostToolUse hook's fork-detached refresh).

        Side effect: also writes the refreshed index to disk so other processes
        in the same project (the PostToolUse hook, the CLI) see the new state.
        """
        if cls._index is None:
            return
        if isinstance(rel, str):
            paths = {rel}
        else:
            paths = set(rel)
        if not paths:
            return
        cls._index.refresh(stale=paths)
        cls._index.save()
        p = cls._index_path()
        cls._index_mtime = p.stat().st_mtime if p.exists() else cls._index_mtime


def _file_part_of_target(target: str) -> str | None:
    """Extract a file path from a target if path-shaped, else None.

    SymbolBody and SymbolOutline accept either a qualified symbol path
    ("wyolet.symbol.cli.main") or a file address ("src/foo.py" or
    "src/foo.py:10-20"). On-the-fly indexing only kicks in for the file
    forms — qualified paths must already resolve through the index.
    """
    head, sep, tail = target.partition(":")
    base = head if (sep and "-" in tail and tail.replace("-", "").isdigit()) else target
    if "/" in base or "\\" in base or "." in base.rsplit("/", 1)[-1]:
        # "." in last segment catches "foo.py" without slashes too. Qualified
        # paths like "pkg.mod.fn" still match — caller falls through to index
        # lookup; ensure_indexed will report not_found if the file doesn't
        # exist on disk, which is the right answer for a typo.
        if Path(base).suffix or "/" in base or "\\" in base:
            return base
    return None


def _ensure_or_error_dict(file: str) -> dict | None:
    """Run ensure_indexed; return an error dict if the file isn't usable."""
    status, file_rel = _State.ensure_indexed(file)
    if status == "ok":
        return None
    if status == "not_found":
        return {"ok": False, "error_code": "not_found", "message": f"file not found: {file}"}
    if status == "outside_project":
        return {"ok": False, "error_code": "invalid_argument", "message": f"file is outside project root: {file}"}
    return {"ok": False, "error_code": "unsupported", "message": f"no language adapter for {file_rel or file}"}


def _ensure_or_error_text(file: str) -> str | None:
    """ensure_indexed for tools that return rendered text. None if ok."""
    err = _ensure_or_error_dict(file)
    if err is None:
        return None
    return _capture_text(_render_patch_error, err["error_code"], err["message"], agent=True)


def _dc(result) -> dict:
    """Dataclass → dict with status promoted to ok + error fields surfaced."""
    d = asdict(result) if is_dataclass(result) else dict(result)
    status = d.get("status")
    ok = status not in ("error",)
    envelope = {"ok": ok, **d}
    if not ok:
        envelope["error_code"] = d.get("error_code")
        envelope["message"] = d.get("message")
    return envelope


# ---------------------------------------------------------- reads


@mcp.tool(name="SearchSymbol", description=descriptions.SEARCH_SYMBOL)
def search_symbol(
    patterns: list[str],
    kind: str | None = None,
    file: str | None = None,
    regex: bool = False,
    fixed: bool = False,
    ignore_case: bool = False,
    limit: int = 100,
) -> dict:
    if regex and fixed:
        return {
            "ok": False,
            "error_code": "invalid_argument",
            "message": "regex and fixed are mutually exclusive",
        }
    root = _State.require_root()
    if file:
        err = _ensure_or_error_dict(file)
        if err is not None:
            return err
    index = _State.require_index()
    hits = search_read(
        index,
        patterns,
        kind=kind,
        file=file,
        regex=regex,
        fixed=fixed,
        ignore_case=ignore_case,
        limit=limit,
    )
    status = "ok" if len(index.symbols) > 0 else "empty"
    return {"ok": True, "count": len(hits), "hits": hits, "index_status": status}


@mcp.tool(name="SymbolBody", description=descriptions.SYMBOL_BODY)
def symbol_body(
    target: str,
    include_refs: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    root = _State.require_root()
    file_part = _file_part_of_target(target)
    if file_part is not None:
        err = _ensure_or_error_dict(file_part)
        if err is not None:
            return err
    index = _State.require_index()
    cache = _State.require_cache()
    try:
        hit = code_read(index, target)
    except CodeNotFound as e:
        return {"ok": False, "error_code": "not_found", "message": str(e)}
    except CodeAmbiguous as e:
        return {
            "ok": False,
            "error_code": "ambiguous",
            "message": str(e),
            "candidates": e.candidates,
        }

    body_lines = hit["body"].splitlines(keepends=True)
    total_lines = len(body_lines)
    start = max(0, offset)
    end = total_lines if limit is None else min(total_lines, start + max(0, limit))
    sliced_body = "".join(body_lines[start:end])
    truncated = (start, end) != (0, total_lines)

    abs_start = hit["start_line"] + start
    abs_end = hit["start_line"] + end - 1 if end > start else hit["start_line"] + start

    record_served(
        cache,
        project_root=root,
        file_rel=hit["file"],
        start_line=abs_start,
        end_line=abs_end,
    )
    out = {**hit, "body": sliced_body, "total_lines": total_lines}
    if truncated:
        out["window"] = {"offset": start, "limit": end - start, "abs_lines": (abs_start, abs_end)}
    if not include_refs:
        out.pop("refs", None)
    return {"ok": True, **out}


@mcp.tool(name="SymbolOutline", description=descriptions.SYMBOL_OUTLINE)
def symbol_outline(target: str) -> dict:
    root = _State.require_root()
    file_part = _file_part_of_target(target)
    if file_part is not None:
        err = _ensure_or_error_dict(file_part)
        if err is not None:
            return err
    index = _State.require_index()

    arg = target
    fs_candidate = Path(target)
    if fs_candidate.exists():
        try:
            arg = str(fs_candidate.resolve().relative_to(root))
        except ValueError:
            arg = target

    roots = outline_read(index, arg)
    _prune_empty_children(roots)
    return {"ok": True, "target": arg, "roots": roots}


def _prune_empty_children(nodes: list[dict]) -> None:
    for node in nodes:
        children = node.get("children")
        if children:
            _prune_empty_children(children)
        else:
            node.pop("children", None)


@mcp.tool(name="SymbolCallers", description=descriptions.SYMBOL_CALLERS)
def symbol_callers(name: str) -> dict:
    root = _State.require_root()
    index = _State.require_index()
    hits = callers_read(index, name)
    return {"ok": True, "count": len(hits), "hits": hits}


# ---------------------------------------------------------- writes


def _truncate_diff(diff: str, max_lines: int) -> str | None:
    """Trim a unified diff to a readable size for tool output.

      - max_lines == 0 → omit diff entirely (return None).
      - max_lines < 0  → return the full diff unchanged.
      - otherwise: if diff exceeds max_lines, keep half from each end with a
        '... (N lines omitted; pass diff_lines=0 to skip diff or -1 for full)'
        marker in the middle.
    """
    if max_lines == 0:
        return None
    if max_lines < 0 or not diff:
        return diff
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    head = max_lines // 2
    tail = max_lines - head
    omitted = len(lines) - max_lines
    truncated = (
        lines[:head]
        + [f"... ({omitted} lines omitted; pass diff_lines=-1 for full diff or 0 to skip)"]
        + lines[-tail:]
    )
    return "\n".join(truncated)


@mcp.tool(name="Patch", description=descriptions.PATCH)
def patch(
    file: str,
    range: str,
    content: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    root = _State.require_root()
    err = _ensure_or_error_text(file)
    if err is not None:
        return err
    cache = _State.require_cache()

    req = validate_args(
        file=file,
        raw_range=range,
        content=content,
        project_root=root,
        force=force,
    )
    if isinstance(req, PatchPreflight):
        return _capture_text(_render_patch_error, req.error_code, req.message, agent=True)

    preflight = preflight_patch(req, cache=cache)
    if preflight.status == "needs_read_confirmation":
        # Record the displayed range as if the agent had just Read it. The
        # rendered message includes the current bytes — same proof an explicit
        # Read would carry. Agent's next Patch call (same args) passes
        # preflight without an extra Read or --force.
        record_served(
            cache,
            project_root=root,
            file_rel=req.file_rel,
            start_line=req.line_range[0],
            end_line=req.line_range[1],
        )
        return _capture_text(_render_patch_needs_read, preflight)

    result = apply_patch(req, cache=cache, dry_run=dry_run)
    if result.status == "error":
        return _capture_text(_render_patch_error, result.error_code, result.message, agent=True)

    # Refresh in-RAM index so the next MCP call sees updated byte offsets.
    if result.status == "applied":
        _State.invalidate_file(result.file_rel)

    return _capture_text(_render_patch_agent, result)


@mcp.tool(name="MultiPatch", description=descriptions.MULTI_PATCH)
def multi_patch(
    file: str,
    edits: list[dict],
    force: bool = False,
    dry_run: bool = False,
) -> str:
    root = _State.require_root()
    err = _ensure_or_error_text(file)
    if err is not None:
        return err
    cache = _State.require_cache()

    file_abs = (root / file).resolve()
    try:
        file_rel = str(file_abs.relative_to(root))
    except ValueError:
        return _capture_text(
            _render_patch_error, "invalid_argument",
            f"file {file!r} is outside project root", agent=True,
        )

    result = apply_patch_multi(
        file_abs=file_abs,
        file_rel=file_rel,
        raw_edits=edits,
        cache=cache,
        dry_run=dry_run,
        force=force,
    )

    if result.status == "applied":
        _State.invalidate_file(result.file_rel)
    elif result.status == "needs_read_confirmation":
        # Same trick as single Patch: record the displayed ranges so the
        # agent's retry passes preflight on its own.
        for u in result.unconfirmed:
            try:
                start_s, end_s = u["range"].split("-", 1)
                record_served(
                    cache,
                    project_root=root,
                    file_rel=result.file_rel,
                    start_line=int(start_s),
                    end_line=int(end_s),
                )
            except (ValueError, KeyError):
                continue

    return _format_multi_patch_result(result)


def _format_multi_patch_result(r) -> str:
    lines = [f"status: {r.status}", f"file: {r.file_rel}"]
    if r.error_code:
        lines.append(f"error_code: {r.error_code}")
    if r.message:
        lines.append(f"message: {r.message}")
    if r.unconfirmed:
        lines.append("unconfirmed:")
        for u in r.unconfirmed:
            lines.append(f"  - edit {u['edit_idx']}: range {u['range']}")
    if r.per_edit:
        lines.append(f"edits: {len(r.per_edit)}")
        for i, e in enumerate(r.per_edit):
            lines.append(
                f"  - {i}: {e['addressed_by']} "
                f"lines {e['before_lines'][0]}-{e['before_lines'][1]} "
                f"→ {e['after_lines'][0]}-{e['after_lines'][1]}"
            )
    if r.diff:
        lines.append("\n--- DIFF ---")
        lines.append(r.diff.rstrip())
        lines.append("--- END ---")
    return "\n".join(lines)


@mcp.tool(name="DeleteSymbol", description=descriptions.DELETE_SYMBOL)
def delete_symbol(
    target: str,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    root = _State.require_root()
    index = _State.require_index()
    cache = _State.require_cache()

    req = resolve_delete_symbol(index, target, root, force=force)
    if not isinstance(req, DeleteSymbolRequest):
        # req is a DeleteSymbolResult with error.
        return _capture_text(_render_delete_error, req, agent=True)
    result = apply_delete_symbol(req, cache=cache, dry_run=dry_run)
    if result.status == "error":
        return _capture_text(_render_delete_error, result, agent=True)
    if result.status == "applied" and result.file_rel:
        _State.invalidate_file(result.file_rel)
    return _capture_text(_render_delete_agent, result)


@mcp.tool(name="InsertSymbol", description=descriptions.INSERT_SYMBOL)
def insert_symbol(
    target: str,
    position: str,
    content: str,
    reindent: bool = True,
    dry_run: bool = False,
) -> str:
    if position not in ("before", "after", "start", "end"):
        return (
            "status: error\n"
            "error_code: invalid_argument\n"
            f"message: position must be one of before|after|start|end, got {position!r}"
        )
    # Auto-append trailing newline so inserted symbols don't jam against the
    # next line. Patch is left raw — bytes there are user-controlled.
    if content and not content.endswith("\n"):
        content = content + "\n"

    root = _State.require_root()
    index = _State.require_index()
    cache = _State.require_cache()

    req = resolve_insert_symbol(
        index, target, position, content, root, reindent=reindent  # type: ignore[arg-type]
    )
    if not isinstance(req, InsertSymbolRequest):
        # req is an InsertSymbolResult with error.
        return _capture_text(_render_insert, req, format="rich", agent=True)
    result = apply_insert_symbol(req, cache=cache, dry_run=dry_run)
    if result.status == "applied" and result.file_rel:
        _State.invalidate_file(result.file_rel)
    return _capture_text(_render_insert, result, format="rich", agent=True)


@mcp.tool(name="RenameSymbol", description=descriptions.RENAME_SYMBOL)
def rename_symbol(
    target: str,
    new_name: str,
    dry_run: bool = False,
) -> str:
    root = _State.require_root()
    index = _State.require_index()

    req = resolve_rename_symbol(index, target, new_name, root)
    if not isinstance(req, RenameSymbolRequest):
        return _capture_text(_render_rename_error, req, agent=True)
    result = apply_rename_symbol(
        req,
        project_root=root,
        dry_run=dry_run,
    )
    if result.status == "error":
        return _capture_text(_render_rename_error, result, agent=True)
    if result.status == "applied":
        touched = {pf.file for pf in result.per_file if getattr(pf, "file", None)}
        if not touched and result.declaring_file:
            touched = {result.declaring_file}
        _State.invalidate_file(touched)
    return _capture_text(_render_rename_agent, result)


@mcp.tool(
    name="Undo",
    description=(
        "Revert the most recent RenameSymbol or ReplaceSymbol transaction. "
        "Restores every file in that op from its captured pre-image (atomic). "
        "Operates on the persisted transaction log in .symbol/transactions/. "
        "Returns the op name, subject, and list of files restored."
    ),
)
def undo() -> dict:
    root = _State.require_root()
    result = _undo_last(root)
    if result.status == "undone":
        _State.invalidate_file(set(result.files_restored))
    return {
        "ok": result.status != "error",
        "status": result.status,
        "transaction_id": result.transaction_id,
        "op": result.op,
        "subject": result.subject,
        "files_restored": list(result.files_restored),
        "files_skipped": list(result.files_skipped),
        "error_code": result.error_code,
        "message": result.message,
    }


@mcp.tool(name="ReplaceSymbol", description=descriptions.REPLACE_SYMBOL)
def replace_symbol(
    target: str,
    content: str,
    dry_run: bool = False,
) -> str:
    # Auto-append trailing newline so the replacement doesn't jam against the
    # next sibling symbol if the agent omits it.
    if content and not content.endswith("\n"):
        content = content + "\n"

    root = _State.require_root()
    index = _State.require_index()

    req = resolve_replace_symbol(index, target, content, root)
    if not isinstance(req, ReplaceSymbolRequest):
        return _capture_text(_render_replace_error, req, agent=True)
    result = apply_replace_symbol(
        req,
        project_root=root,
        dry_run=dry_run,
    )
    if result.status == "error":
        return _capture_text(_render_replace_error, result, agent=True)
    if result.status == "applied":
        touched = {pf.file for pf in result.per_file if getattr(pf, "file", None)}
        if not touched and result.declaring_file:
            touched = {result.declaring_file}
        _State.invalidate_file(touched)
    return _capture_text(_render_replace_agent, result)




# ---------------------------------------------------------- entry


def serve(project_root: str | Path = ".") -> None:
    """Blocking stdio server loop. Call from CLI subcommand."""
    import sys as _sys

    _State.initialize(Path(project_root))
    idx = _State._index
    root = _State.project_root
    source = getattr(_State, "_index_source", "?")
    n_symbols = len(idx.symbols) if idx else 0
    n_files = len(idx.files) if idx else 0
    status = "empty" if n_symbols == 0 else "ok"
    print(
        f"[symbol-mcp] root={root} index={source} files={n_files} "
        f"symbols={n_symbols} status={status}",
        file=_sys.stderr,
        flush=True,
    )
    mcp.run(transport="stdio")
