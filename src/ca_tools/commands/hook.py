"""PreToolUse hook entry point — soft nudge variant.

Reads Claude Code's hook JSON from stdin, asks the symbol index whether the
intercepted tool call has a better MCP-tool alternative, and either:
  - exits 0 silently (no opinion — the tool proceeds normally)
  - exits 0 with a JSON `additionalContext` payload that allows the tool AND
    injects a short suggestion into the model's context for next-turn use

Never blocks. The tool always runs. The nudge informs the *next* decision.

Decision is index-driven, not pattern-matched:
  - Read on a file the index knows about → mention SymbolOutline / SymbolBody.
  - Grep whose pattern resolves to indexed declarations
    → mention SearchSymbol / SymbolCallers / SymbolBody with a real path.

If the index isn't present (no `.ca-tools/symbol_index.pkl`), the hook is a
no-op. It never triggers a build.
"""

import json
import os
import sys
from pathlib import Path

from ca_tools.reads.search import search as index_search
from ca_tools.shared.symbol_index import SymbolIndex


def run() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    cwd = payload.get("cwd") or os.getcwd()

    project_root = _find_project_root(Path(cwd))
    if project_root is None:
        sys.exit(0)

    index = SymbolIndex.load(project_root)
    if index is None:
        sys.exit(0)

    if tool_name == "Read":
        message = _read_suggestion(index, project_root, tool_input)
    elif tool_name == "Grep":
        message = _grep_suggestion(index, tool_input)
    else:
        message = None

    if message is None:
        sys.exit(0)

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
    sys.exit(0)


def _find_project_root(start: Path) -> Path | None:
    p = start.resolve()
    while p != p.parent:
        if (p / ".ca-tools").exists() or (p / "pyproject.toml").exists() or (p / ".git").exists():
            return p
        p = p.parent
    return None


def _read_suggestion(index: SymbolIndex, project_root: Path, tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return None
    if tool_input.get("offset") or tool_input.get("limit"):
        return None

    try:
        rel = str(Path(file_path).resolve().relative_to(project_root))
    except ValueError:
        return None

    lang = index.language_of_file(rel)
    if lang is None:
        return None

    return (
        f'ca-tools: "{rel}" is in the symbol index ({lang}). '
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
        f'ca-tools: "{pattern}" matches {n}{more} indexed declaration(s) '
        f"(e.g. {sample}). For symbol questions, prefer these next time:\n"
        f'  • SearchSymbol(patterns=["{pattern}"]) — declarations with qualified paths\n'
        f'  • SymbolCallers(name="{leaf}") — every containing symbol that references it\n'
        f'  • SymbolBody(target="{sample}") — one symbol + used imports + refs'
    )


def _search_safe(index: SymbolIndex, pattern: str, *, regex: bool) -> list[dict]:
    try:
        return index_search(index, [pattern], regex=regex, limit=5)
    except Exception:
        return []
