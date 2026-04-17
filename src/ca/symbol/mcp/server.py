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

mcp = FastMCP("symbol")


class _State:
    """Process-wide state. Set once by ``serve``, read by tool handlers."""

    project_root: Path | None = None
    read_cache: InMemoryReadCache | None = None

    @classmethod
    def initialize(cls, project_root: Path) -> None:
        cls.project_root = project_root.resolve()
        cls.read_cache = InMemoryReadCache()
        build_workspace(cls.project_root)
        get_or_build_index(cls.project_root)

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
    index, _ = get_or_build_index(root)
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
def symbol_body(target: str, include_refs: bool = True) -> dict:
    root = _State.require_root()
    index, _ = get_or_build_index(root)
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

    record_served(
        cache,
        project_root=root,
        file_rel=hit["file"],
        start_line=hit["start_line"],
        end_line=hit["end_line"],
    )
    if not include_refs:
        hit = {**hit, "refs": []}
    return {"ok": True, **hit}


@mcp.tool(name="SymbolOutline", description=descriptions.SYMBOL_OUTLINE)
def symbol_outline(target: str) -> dict:
    root = _State.require_root()
    index, _ = get_or_build_index(root)

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
    index, _ = get_or_build_index(root)
    hits = callers_read(index, name)
    return {"ok": True, "count": len(hits), "hits": hits}


# ---------------------------------------------------------- writes


@mcp.tool(name="Patch", description=descriptions.PATCH)
def patch(
    file: str,
    range: str,
    content: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
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
        return {
            "ok": False,
            "error_code": req.error_code,
            "message": req.message,
        }

    preflight = preflight_patch(req, cache=cache)
    if preflight.status == "needs_read_confirmation":
        return {
            "ok": False,
            "error_code": "needs_read_confirmation",
            "message": (
                f"{req.file_rel} lines {req.line_range[0]}-{req.line_range[1]} "
                "haven't been read in this session"
            ),
            "hint": "Call SymbolBody or Read on this range, then retry.",
        }

    result = apply_patch(req, cache=cache, dry_run=dry_run)
    if result.status == "error":
        return {
            "ok": False,
            "error_code": result.error_code,
            "message": result.message,
        }
    return {
        "ok": True,
        "status": result.status,
        "file": result.file_rel,
        "before_range": list(result.before_range),
        "after_range": list(result.after_range),
        "lines_removed": result.lines_removed,
        "lines_added": result.lines_added,
        "diff": result.diff,
    }


@mcp.tool(name="DeleteSymbol", description=descriptions.DELETE_SYMBOL)
def delete_symbol(
    qualified_path: str,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    root = _State.require_root()
    index, _ = get_or_build_index(root)
    cache = _State.require_cache()

    req = resolve_delete_symbol(index, qualified_path, root, force=force)
    if not isinstance(req, DeleteSymbolRequest):
        return _dc(req)
    result = apply_delete_symbol(req, cache=cache, dry_run=dry_run)
    return _dc(result)


@mcp.tool(name="InsertSymbol", description=descriptions.INSERT_SYMBOL)
def insert_symbol(
    anchor: str,
    position: str,
    content: str,
    reindent: bool = True,
    dry_run: bool = False,
) -> dict:
    if position not in ("before", "after", "start", "end"):
        return {
            "ok": False,
            "error_code": "invalid_argument",
            "message": f"position must be one of before|after|start|end, got {position!r}",
        }
    root = _State.require_root()
    index, _ = get_or_build_index(root)
    cache = _State.require_cache()

    req = resolve_insert_symbol(
        index, anchor, position, content, root, reindent=reindent  # type: ignore[arg-type]
    )
    if not isinstance(req, InsertSymbolRequest):
        return _dc(req)
    result = apply_insert_symbol(req, cache=cache, dry_run=dry_run)
    return _dc(result)


@mcp.tool(name="RenameSymbol", description=descriptions.RENAME_SYMBOL)
def rename_symbol(
    qualified_path: str,
    new_name: str,
    dry_run: bool = False,
    allow_dirty: bool = False,
    force_no_vcs: bool = False,
) -> dict:
    root = _State.require_root()
    index, _ = get_or_build_index(root)

    req = resolve_rename_symbol(index, qualified_path, new_name, root)
    if not isinstance(req, RenameSymbolRequest):
        return _dc(req)
    result = apply_rename_symbol(
        req,
        project_root=root,
        dry_run=dry_run,
        allow_dirty=allow_dirty,
        force_no_vcs=force_no_vcs,
    )
    return _dc(result)


@mcp.tool(name="ReplaceSymbol", description=descriptions.REPLACE_SYMBOL)
def replace_symbol(
    qualified_path: str,
    content: str,
    dry_run: bool = False,
    allow_dirty: bool = False,
    force_no_vcs: bool = False,
) -> dict:
    root = _State.require_root()
    index, _ = get_or_build_index(root)

    req = resolve_replace_symbol(index, qualified_path, content, root)
    if not isinstance(req, ReplaceSymbolRequest):
        return _dc(req)
    result = apply_replace_symbol(
        req,
        project_root=root,
        dry_run=dry_run,
        allow_dirty=allow_dirty,
        force_no_vcs=force_no_vcs,
    )
    return _dc(result)


# ---------------------------------------------------------- entry


def serve(project_root: str | Path = ".") -> None:
    """Blocking stdio server loop. Call from CLI subcommand."""
    _State.initialize(Path(project_root))
    mcp.run(transport="stdio")
