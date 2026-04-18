"""MCP server entry point.

Single-root v1: one server = one project. Builds a Workspace + warm
SymbolIndex + InMemoryReadCache once at startup; every tool call reuses
them. Multi-root lifespan-scoped sessions are a follow-up.
"""

from dataclasses import asdict, is_dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ca.symbol.caches import InMemoryReadCache, record_served
from ca.symbol.mcp import descriptions
from ca.symbol.reads.callers import callers as callers_read
from ca.symbol.reads.code import CodeAmbiguous, CodeNotFound, code as code_read
from ca.symbol.reads.outline import outline as outline_read
from ca.symbol.reads.search import search as search_read
from ca.symbol.shared.symbol_index import get_or_build_index
from ca.symbol.shared.workspace import build_workspace
from ca.symbol.writes.delete_symbol import (
    DeleteSymbolRequest,
    apply_delete_symbol,
    resolve_delete_symbol,
)
from ca.symbol.writes.insert_symbol import (
    InsertSymbolRequest,
    apply_insert_symbol,
    resolve_insert_symbol,
)
from ca.symbol.writes.patch import (
    PatchPreflight,
    apply_patch,
    preflight_patch,
    validate_args,
)
from ca.symbol.writes.rename_symbol import (
    RenameSymbolRequest,
    apply_rename_symbol,
    resolve_rename_symbol,
)
from ca.symbol.writes.replace_symbol import (
    ReplaceSymbolRequest,
    apply_replace_symbol,
    resolve_replace_symbol,
)

import contextlib
import io

from ca.symbol.commands.delete_symbol import (
    _render_agent as _render_delete_agent,
    _render_error as _render_delete_error,
)
from ca.symbol.commands.insert_symbol import _render as _render_insert
from ca.symbol.commands.patch import (
    _render_error as _render_patch_error,
    _render_needs_confirmation_agent as _render_patch_needs_read,
    _render_result_agent as _render_patch_agent,
)
from ca.symbol.commands.rename_symbol import (
    _render_agent as _render_rename_agent,
    _render_error as _render_rename_error,
)
from ca.symbol.commands.replace_symbol import (
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
        return cls.require_root() / ".ca" / "symbol_index.msgpack.zst"

    @classmethod
    def _load_index(cls) -> None:
        idx, _ = get_or_build_index(cls.require_root())
        cls._index = idx
        p = cls._index_path()
        cls._index_mtime = p.stat().st_mtime if p.exists() else None

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
    return {"ok": True, "count": len(hits), "hits": hits}


@mcp.tool(name="SymbolBody", description=descriptions.SYMBOL_BODY)
def symbol_body(
    target: str,
    include_refs: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    root = _State.require_root()
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
        out["refs"] = []
    return {"ok": True, **out}


@mcp.tool(name="SymbolOutline", description=descriptions.SYMBOL_OUTLINE)
def symbol_outline(target: str) -> dict:
    root = _State.require_root()
    index = _State.require_index()

    arg = target
    fs_candidate = Path(target)
    if fs_candidate.exists():
        try:
            arg = str(fs_candidate.resolve().relative_to(root))
        except ValueError:
            arg = target

    roots = outline_read(index, arg)
    return {"ok": True, "target": arg, "roots": roots}


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
        return _capture_text(_render_patch_needs_read, preflight)

    result = apply_patch(req, cache=cache, dry_run=dry_run)
    if result.status == "error":
        return _capture_text(_render_patch_error, result.error_code, result.message, agent=True)

    # Refresh in-RAM index so the next MCP call sees updated byte offsets.
    if result.status == "applied":
        _State.invalidate_file(result.file_rel)

    return _capture_text(_render_patch_agent, result)






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
    allow_dirty: bool = False,
    force_no_vcs: bool = False,
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
        allow_dirty=allow_dirty,
        force_no_vcs=force_no_vcs,
    )
    if result.status == "error":
        return _capture_text(_render_rename_error, result, agent=True)
    if result.status == "applied":
        touched = {pf.file for pf in result.per_file if getattr(pf, "file", None)}
        if not touched and result.declaring_file:
            touched = {result.declaring_file}
        _State.invalidate_file(touched)
    return _capture_text(_render_rename_agent, result)




@mcp.tool(name="ReplaceSymbol", description=descriptions.REPLACE_SYMBOL)
def replace_symbol(
    target: str,
    content: str,
    dry_run: bool = False,
    allow_dirty: bool = False,
    force_no_vcs: bool = False,
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
        allow_dirty=allow_dirty,
        force_no_vcs=force_no_vcs,
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
    _State.initialize(Path(project_root))
    mcp.run(transport="stdio")
