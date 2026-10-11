"""Tool descriptions shown to the agent at tool-list time.

The text lives in ``data/mcp_tools.toml`` so the Rust server embeds the
same file and the two cannot drift.
"""

import tomllib
from importlib.resources import files

_TOOLS = tomllib.loads(
    files("wyolet.symbol").joinpath("data", "mcp_tools.toml").read_text(encoding="utf-8")
)


def _description(tool: str) -> str:
    return _TOOLS[tool]["description"]


SEARCH_SYMBOL = _description("SearchSymbol")
SYMBOL_BODY = _description("SymbolBody")
SYMBOL_OUTLINE = _description("SymbolOutline")
SYMBOL_CALLERS = _description("SymbolCallers")
PATCH = _description("Patch")
MULTI_PATCH = _description("MultiPatch")
DELETE_SYMBOL = _description("DeleteSymbol")
INSERT_SYMBOL = _description("InsertSymbol")
RENAME_SYMBOL = _description("RenameSymbol")
REFRESH = _description("Refresh")
UNDO = _description("Undo")
REPLACE_SYMBOL = _description("ReplaceSymbol")
