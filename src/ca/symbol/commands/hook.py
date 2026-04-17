"""`symbol hook` — Claude Code hook entry point.

Single command, dispatches on `hook_event_name` from the JSON payload:

  PreToolUse  (Grep / Read / Edit)
    → soft nudge: emit additionalContext suggesting MCP-tool alternatives
      when the target is in the symbol index. Tool always proceeds.

  PostToolUse (Edit / Write) on an indexed `.py` file that succeeded
    → refresh the index for that file. Silent side effect.

If the index isn't present (no `.ca/symbol_index.msgpack.zst`), the hook
is a no-op for both cases. It never triggers a build.
"""

import json
import os
import sys
from pathlib import Path

from ca.symbol.reads.search import search as index_search
from ca.symbol.shared.symbol_index import SymbolIndex


def run() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    event = payload.get("hook_event_name", "")
    cwd = payload.get("cwd") or os.getcwd()
    project_root = _find_project_root(Path(cwd))
    if project_root is None:
        sys.exit(0)

    if event == "PreToolUse":
        # Synchronous — must finish before tool runs to inject additionalContext.
        index = SymbolIndex.load(project_root)
        if index is None:
            sys.exit(0)
        _handle_pre(index, project_root, payload)
    elif event == "PostToolUse":
        # Detach early — fork BEFORE loading the 16MB index, so the parent
        # exits in ~50ms and the child handles the heavy lift in background.
        _handle_post_detached(project_root, payload)
    sys.exit(0)


# --------------------------------------------------------------------------
# PreToolUse: soft nudge
# --------------------------------------------------------------------------

def _handle_pre(index: SymbolIndex, project_root: Path, payload: dict) -> None:
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    if tool_name == "Read":
        message = _read_suggestion(index, project_root, tool_input)
    elif tool_name == "Grep":
        message = _grep_suggestion(index, tool_input)
    elif tool_name == "Edit":
        message = _edit_suggestion(index, project_root, tool_input)
    else:
        return

    if message is None:
        return

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": message,
            }
        },
        sys.stdout,
    )


def _read_suggestion(index: SymbolIndex, project_root: Path, tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return None
    if tool_input.get("offset") or tool_input.get("limit"):
        return None

    rel = _to_rel(project_root, file_path)
    if rel is None:
        return None

    lang = index.language_of_file(rel)
    if lang is None:
        return None

    return (
        f'symbol: "{rel}" is in the symbol index ({lang}). '
        f"For Python files, prefer these next time you only need part of the file:\n"
        f'  • SymbolOutline(target="{rel}") — structural tree, no bodies\n'
        f'  • SymbolBody(target="<qualified.path>") — one symbol + used imports'
    )


def _grep_suggestion(index: SymbolIndex, tool_input: dict) -> str | None:
    pattern = (tool_input.get("pattern") or "").strip()
    if not pattern:
        return None

    hits = _search_safe(index, pattern, regex=False)
    if not hits:
        hits = _search_safe(index, pattern, regex=True)
    if not hits:
        return None

    sample = hits[0]["path"]
    leaf = sample.rsplit(".", 1)[-1]
    n = len(hits)
    more = "+" if n >= 5 else ""

    return (
        f'symbol: "{pattern}" matches {n}{more} indexed declaration(s) '
        f"(e.g. {sample}). For symbol questions, prefer these next time:\n"
        f'  • SearchSymbol(patterns=["{pattern}"]) — declarations with qualified paths\n'
        f'  • SymbolCallers(name="{leaf}") — every containing symbol that references it\n'
        f'  • SymbolBody(target="{sample}") — one symbol + used imports + refs'
    )


def _edit_suggestion(index: SymbolIndex, project_root: Path, tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return None

    rel = _to_rel(project_root, file_path)
    if rel is None:
        return None

    lang = index.language_of_file(rel)
    if lang is None:
        return None

    return (
        f'symbol: "{rel}" is in the symbol index ({lang}). For edits to indexed '
        f"Python files, these tools provide guarantees Edit cannot:\n"
        f"  • Patch(file=..., range=A-B, content=...) — byte-range edit, no old_string round-trip\n"
        f'  • ReplaceSymbol(qualified_path=..., content=...) — whole symbol; parses before commit; rewrites callers if leaf renamed\n'
        f'  • InsertSymbol(anchor=..., position=before|after|start|end, content=...) — auto-indented, structural\n'
        f"  • DeleteSymbol(qualified_path=...) — refuses if callers exist\n"
        f'  • RenameSymbol(qualified_path=..., new_name=...) — atomic across files, git-checkpointed\n'
        f"Edit still works; pick it when the change crosses symbol boundaries or touches non-symbol regions."
    )


def _search_safe(index: SymbolIndex, pattern: str, *, regex: bool) -> list[dict]:
    try:
        return index_search(index, [pattern], regex=regex, limit=5)
    except Exception:
        return []


# --------------------------------------------------------------------------
# PostToolUse: incremental index refresh
# --------------------------------------------------------------------------

def _handle_post_detached(project_root: Path, payload: dict) -> None:
    """Decide whether to refresh, then fork-detach. Parent returns in ~50ms.

    The index load + save (~1-2s combined) happens entirely in the child,
    so the agent isn't blocked. Failures in the child are swallowed.
    """
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        return

    tool_input = payload.get("tool_input", {}) or {}
    tool_response = payload.get("tool_response", {}) or {}

    if isinstance(tool_response, dict) and tool_response.get("error"):
        return

    file_path = tool_input.get("file_path") or ""
    if not file_path or not file_path.endswith(".py"):
        return

    rel = _to_rel(project_root, file_path)
    if rel is None:
        return

    # POSIX fork-detach. Windows falls through to synchronous.
    if hasattr(os, "fork"):
        pid = os.fork()
        if pid > 0:
            return  # parent returns; Claude Code unblocks the agent
        os.setsid()
        with open(os.devnull, "w") as dn:
            os.dup2(dn.fileno(), sys.stdout.fileno())
            os.dup2(dn.fileno(), sys.stderr.fileno())

    try:
        index = SymbolIndex.load(project_root)
        if index is None:
            return
        if (project_root / rel).exists():
            index.refresh(stale={rel})
        else:
            index.refresh(deleted={rel})
        index.save()
    except Exception:
        pass

    if hasattr(os, "fork"):
        os._exit(0)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _find_project_root(start: Path) -> Path | None:
    p = start.resolve()
    while p != p.parent:
        if (p / ".ca").exists() or (p / "pyproject.toml").exists() or (p / ".git").exists():
            return p
        p = p.parent
    return None


def _to_rel(project_root: Path, file_path: str) -> str | None:
    try:
        return str(Path(file_path).resolve().relative_to(project_root))
    except ValueError:
        return None
