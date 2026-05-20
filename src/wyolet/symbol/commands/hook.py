"""`symbol hook` — Claude Code hook entry point.

Single command, dispatches on `hook_event_name` from the JSON payload:

  PreToolUse  (Grep / Read / Edit)
    → Default (nudge mode): emit `additionalContext` so the model sees the
      MCP-tool alternatives in its next turn. Tool proceeds.
    → With `--enforce`: exit 2 with the suggestion on stderr. Claude Code
      blocks the tool call and shows stderr to the model as the deny reason,
      forcing reroute. Bench measurement showed soft nudges are often
      ignored against trained habit; --enforce is the escalation knob.

  PostToolUse (Edit / Write) on an indexed `.py` file that succeeded
    → refresh the index for that file. Silent side effect, fork-detached.

If the index isn't present (no `.symbol/symbol_index.msgpack.zst`), the hook
is a no-op for both cases. It never triggers a build.
"""

import json
import os
import sys
from pathlib import Path

from wyolet.symbol.adapters.registry import default_registry
from wyolet.symbol.reads.search import search as index_search
from wyolet.symbol.shared.symbol_index import SymbolIndex


def run(enforce: bool = False) -> None:
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
        # Synchronous — must finish before tool runs.
        index = SymbolIndex.load(project_root)
        if index is None:
            sys.exit(0)
        _handle_pre(index, project_root, payload, enforce=enforce)
    elif event == "PostToolUse":
        # Detach early — fork BEFORE loading the 16MB index, so the parent
        # exits in ~50ms and the child handles the heavy lift in background.
        _handle_post_detached(project_root, payload)
    sys.exit(0)


# --------------------------------------------------------------------------
# PreToolUse: nudge (default) or block (--enforce)
# --------------------------------------------------------------------------

def _handle_pre(index: SymbolIndex, project_root: Path, payload: dict, *, enforce: bool) -> None:
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    # File-bearing tools: skip non-code files entirely (md, json, settings,
    # arbitrary text). The language registry — driven by linguist content
    # sniffing, not just extension — is the source of truth for "code we own".
    if tool_name in ("Read", "Edit", "Write"):
        file_path = (tool_input.get("file_path") or "").strip()
        if not file_path:
            return
        abs_path = Path(file_path)
        if not abs_path.is_file():
            return
        if not default_registry().supports(abs_path):
            return

    if tool_name == "Read":
        message = _read_suggestion(index, project_root, tool_input)
    elif tool_name == "Grep":
        message = _grep_suggestion(index, tool_input)
    elif tool_name == "Edit":
        message = _edit_suggestion(index, project_root, tool_input)
    elif tool_name == "Write":
        message = _write_suggestion(index, project_root, tool_input)
    else:
        return

    if message is None:
        return

    if enforce:
        # Exit 2 + stderr: Claude Code blocks the tool and surfaces stderr
        # to the model as the deny reason. The model must reroute.
        print(message, file=sys.stderr)
        sys.exit(2)

    # Default soft nudge: tool proceeds, model sees suggestion in next turn.
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
    rel = _to_rel(project_root, file_path)
    if rel is None:
        return None

    lang = _detect_language(index, project_root, rel)

    # Small-file bypass: under ~80 lines the MCP overhead exceeds the win.
    if _too_small(project_root, rel):
        return None

    # Note: offset/limit bypass intentionally NOT honored. SymbolBody can take
    # a "file:start-end" address for any arbitrary line range, so partial Read
    # has no legitimate use case for indexed files that SymbolBody can't cover.
    return (
        f'symbol: "{rel}" is indexed ({lang}). Read returns raw file lines — '
        f"SymbolBody returns the same range with structural awareness (used imports, "
        f"refs, declared kind) at the same or lower token cost. Use:\n"
        f'  • SymbolOutline(target="{rel}") — file structure, no bodies\n'
        f'  • SymbolBody(target="<qualified.path>") — one named symbol + imports + refs\n'
        f'  • SymbolBody(target="{rel}:START-END") — arbitrary line range with refs\n'
        f"Read only when you genuinely need the unstructured file content (top-level "
        f"comments, module-level constants, or non-Python regions)."
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
        f'symbol: "{pattern}" is the name of {n}{more} indexed declaration(s) '
        f"(e.g. {sample}). Grep returns raw line matches mixed with comments, "
        f"strings, and unrelated occurrences — typically 5-10× the noise of a "
        f"symbol-aware lookup. Use:\n"
        f'  • SearchSymbol(patterns=["{pattern}"]) — declarations only\n'
        f'  • SymbolCallers(name="{leaf}") — every containing symbol that references it\n'
        f'  • SymbolBody(target="{sample}") — the body, used imports, refs\n'
        f"Re-issue Grep only for non-identifier text (TODOs, doc hunts, regex)."
    )


def _write_suggestion(index: SymbolIndex, project_root: Path, tool_input: dict) -> str | None:
    """Only fires when Write would OVERWRITE an existing indexed .py file.

    Creating new files passes through silently — Write is the right tool for
    that. The bypass we close: Write replacing the entire content of an
    already-indexed file as an end-run around the Edit nudge.
    """
    file_path = tool_input.get("file_path") or ""
    rel = _to_rel(project_root, file_path)
    if rel is None:
        return None

    abs_path = project_root / rel
    if not abs_path.exists():
        return None  # creating, not overwriting — pass through

    lang = _detect_language(index, project_root, rel)

    if _too_small(project_root, rel):
        return None

    return (
        f'symbol: "{rel}" already exists and is indexed ({lang}). Write would '
        f"overwrite it wholesale, losing the per-symbol structural edit guarantees. "
        f"For changes to an existing indexed file, use:\n"
        f"  • Patch(file=..., range=A-B, content=...) — byte-range edit, atomic\n"
        f'  • ReplaceSymbol(qualified_path=..., content=...) — whole symbol; parse-validated\n'
        f'  • InsertSymbol(anchor=..., position=..., content=...) — structural insert\n'
        f"Write is correct only for *new* files (it's a no-op nudge in that case)."
    )



def _edit_suggestion(index: SymbolIndex, project_root: Path, tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path") or ""
    rel = _to_rel(project_root, file_path)
    if rel is None:
        return None

    lang = _detect_language(index, project_root, rel)

    if _too_small(project_root, rel):
        return None

    return (
        f'symbol: "{rel}" is indexed ({lang}). Edit requires re-sending the existing '
        f"content for disambiguation (~200 extra tokens per call) and provides no "
        f"parse validation. The MCP write tools cover every case Edit covers:\n"
        f"  • Patch(file=..., range=A-B, content=...) — byte-range, no old_string round-trip (handles ALL non-symbol edits)\n"
        f'  • ReplaceSymbol(qualified_path=..., content=...) — whole symbol; parse-validated; rewrites callers if leaf renamed\n'
        f'  • InsertSymbol(anchor=..., position=before|after|start|end, content=...) — auto-indented, structural\n'
        f"  • DeleteSymbol(qualified_path=...) — refuses if callers exist\n"
        f'  • RenameSymbol(qualified_path=..., new_name=...) — atomic across files, transactional (Undo-able)\n'
        f"Use Edit only on files symbol does not index."
    )



def _detect_language(index: SymbolIndex, project_root: Path, rel: str) -> str:
    """Resolve a label for the file's language.

    Index first (free); fall back to linguist via the adapter registry for
    files the index hasn't seen yet (e.g. just-created via Write). Returns
    "code" if neither knows — caller is gated upstream so this only fires
    for files already proven supported.
    """
    lang = index.language_of_file(rel)
    if lang:
        return lang
    try:
        adapter = default_registry().for_file(project_root / rel)
        return adapter.lang
    except Exception:
        return "code"


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

    file_path = (tool_input.get("file_path") or "").strip()
    if not file_path:
        return

    abs_path = Path(file_path)
    # On delete the file is gone — refresh handles tombstoning, no support
    # check possible. For create/edit, gate by registry (linguist-aware).
    if abs_path.exists() and not default_registry().supports(abs_path):
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
    """Walk up from `start` looking for a `.symbol/` directory.

    `.symbol/` is the only marker — pyproject.toml and .git would falsely
    match anywhere on the filesystem (notably ~/.claude/...) and trigger
    nudges from outside any instrumented project.
    """
    p = start.resolve()
    while p != p.parent:
        if (p / ".symbol").is_dir():
            return p
        p = p.parent
    return None


def _to_rel(project_root: Path, file_path: str) -> str | None:
    try:
        return str(Path(file_path).resolve().relative_to(project_root))
    except ValueError:
        return None
def _too_small(project_root: Path, rel: str, threshold: int = 80) -> bool:
    """True when the indexed file is below `threshold` lines.

    Below ~80 lines, the per-call MCP overhead exceeds the token-savings win
    over a direct Read/Edit. Bypass the nudge in that range so native tools
    handle small modules without friction.
    """
    try:
        with open(project_root / rel) as f:
            for i, _ in enumerate(f, start=1):
                if i > threshold:
                    return False
            return True
    except Exception:
        return False
