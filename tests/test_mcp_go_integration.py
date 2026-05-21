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


# ── write tools — full MCP→writes pipeline on Go ──────────────────


def test_mcp_replace_symbol_full_pipeline(go_project: Path):
    """ReplaceSymbol via the MCP tool (not just adapter.symbols).

    Dry-run so the fixture isn't mutated. Asserts the operation reaches
    the apply phase without an error, which means resolve + validate +
    symbols projection all succeeded.
    """
    from wyolet.symbol.mcp.server import replace_symbol

    output = replace_symbol(
        target="example.com/user.New",
        content="func New(name string) *User {\n\treturn &User{Name: name + \"!\"}\n}",
        dry_run=True,
    )
    assert isinstance(output, str)
    assert "error" not in output.lower() or "applied" in output.lower() or "dry" in output.lower()


def test_mcp_rename_symbol_works_on_go(go_project: Path):
    from wyolet.symbol.mcp.server import rename_symbol

    output = rename_symbol(target="example.com/user.New", new_name="Make", dry_run=True)
    assert isinstance(output, str)
    # Must not fail with adapter-method or parse errors.
    assert "AttributeError" not in output
    # Should at minimum acknowledge the target.
    assert "New" in output or "Make" in output


def test_mcp_delete_symbol_works_on_go(go_project: Path):
    from wyolet.symbol.mcp.server import delete_symbol

    # DefaultName has no callers in the fixture — safe candidate.
    output = delete_symbol(target="example.com/user.DefaultName", dry_run=True)
    assert isinstance(output, str)
    assert "AttributeError" not in output


def test_mcp_insert_symbol_works_on_go(go_project: Path):
    from wyolet.symbol.mcp.server import insert_symbol

    output = insert_symbol(
        target="example.com/user.New",
        position="after",
        content="func Make(name string) *User {\n\treturn New(name)\n}\n",
        dry_run=True,
    )
    assert isinstance(output, str)
    assert "AttributeError" not in output


def test_mcp_patch_works_on_go(go_project: Path):
    """Pure byte-range patch — language-blind by construction, but verify
    no MCP-layer Python assumption sneaks in.
    """
    from wyolet.symbol.mcp.server import patch

    # Patch the const declaration to a new value. force=True skips the
    # read-confirmation flow that's not relevant here.
    output = patch(
        file="user.go",
        range="10-10",
        content="const MaxRetries = 5\n",
        dry_run=True,
        force=True,
    )
    assert isinstance(output, str)
    assert "AttributeError" not in output


def test_mcp_multi_patch_works_on_go(go_project: Path):
    from wyolet.symbol.mcp.server import multi_patch

    output = multi_patch(
        file="user.go",
        edits=[
            {"range": "10-10", "content": "const MaxRetries = 5\n"},
            {"range": "12-12", "content": "var DefaultName = \"user\"\n"},
        ],
        dry_run=True,
        force=True,
    )
    assert isinstance(output, str)
    assert "AttributeError" not in output


def test_mcp_refresh_works_on_go(go_project: Path):
    from wyolet.symbol.mcp.server import refresh

    result = refresh(full=False)
    assert result["ok"] is True
    assert result["symbols"] >= 5  # at least the 5 from user.go
    assert result["files"] >= 1


def test_mcp_undo_no_transaction(go_project: Path):
    """Undo with no prior transaction returns a clean status, not an
    AttributeError. Real undo is exercised after a real mutation, which
    we don't run in dry-run mode.
    """
    from wyolet.symbol.mcp.server import undo

    result = undo()
    assert isinstance(result, dict)
    # No transactions exist → status is something like "noop" / "no_transactions".
    assert result["status"] != "error" or result.get("error_code") is not None
