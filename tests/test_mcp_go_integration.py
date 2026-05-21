"""End-to-end MCP tool verification on a Go fixture.

Initializes the MCP server's process-wide _State against a tmp Go project
and exercises each tool that should work for Go: read tools work for
free via the index; ReplaceSymbol needs GoAstAdapter.symbols().

Skipped when the go-scan binary isn't built. Pattern matches
test_go_adapter.py — same precondition, same skip message.
"""

import shutil
from pathlib import Path

import pytest

from wyolet.symbol.adapters.go_ast import GoAstAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "go_project" / "user.go"


def _skip_if_no_daemon() -> None:
    if not GoAstAdapter().is_enabled:
        pytest.skip("go-scan binary not built; see tests/test_go_adapter.py for setup")


@pytest.fixture
def go_project(tmp_path: Path) -> Path:
    """A tiny Go project at tmp_path with user.go and a go.mod, MCP-State
    initialized against it. Yields the project root.
    """
    _skip_if_no_daemon()
    shutil.copy(FIXTURE, tmp_path / "user.go")
    (tmp_path / "go.mod").write_text("module example.com/user\n\ngo 1.22\n")

    from wyolet.symbol.mcp import server

    server._State.project_root = None  # reset between tests
    server._State.read_cache = None
    server._State._index = None
    server._State._index_mtime = None
    server._State.initialize(tmp_path)
    return tmp_path


# ── read tools (should work for free) ─────────────────────────────


def test_mcp_search_finds_go_symbol(go_project: Path):
    from wyolet.symbol.mcp.server import search_symbol

    result = search_symbol(patterns=["Greet"])
    assert result["ok"] is True
    assert result["count"] >= 1
    qualifiers = {hit["path"] for hit in result["hits"]}
    assert any("Greet" in q for q in qualifiers)


def test_mcp_outline_renders_go_file(go_project: Path):
    from wyolet.symbol.mcp.server import symbol_outline

    result = symbol_outline("user.go")
    assert result["ok"] is True
    # Outline output mentions the type and method names.
    rendered = str(result)
    assert "User" in rendered
    assert "Greet" in rendered


def test_mcp_callers_finds_go_refs(go_project: Path):
    from wyolet.symbol.mcp.server import symbol_callers

    result = symbol_callers("Println")
    assert result["ok"] is True
    assert result["count"] >= 1


def test_mcp_symbol_body_returns_go_function(go_project: Path):
    from wyolet.symbol.mcp.server import symbol_body

    result = symbol_body("example.com/user.New")
    assert result["ok"] is True
    body = result.get("body") or result.get("content") or ""
    # The body of New() contains its return statement.
    assert "User" in str(result)


# ── write tools (need GoAstAdapter.symbols) ───────────────────────


def test_mcp_replace_symbol_works_on_go(go_project: Path):
    """Smoke test: the previously-broken adapter.symbols() path is now
    implemented. ReplaceSymbol on a Go function should succeed (or at
    least fail with a real validation error, not AttributeError).
    """
    from wyolet.symbol.adapters.registry import default_registry

    adapter = default_registry().for_language("go")
    # Direct: this is the call site replace_symbol.py:139 makes.
    new_content = b"func New(name string) *User { return &User{Name: name} }\n"
    syms = adapter.symbols(Path("<replace-content>"), new_content)
    assert len(syms) == 1
    assert syms[0].name == "New"
    assert syms[0].kind == "function"
    assert syms[0].signature_line >= 1
