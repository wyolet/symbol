"""End-to-end Go indexing tests via the JSON-RPC daemon.

Skipped automatically when the bundled ``go-scan`` binary isn't built,
so the Python test suite stays green on machines without Go installed.
Run ``go build`` inside ``src/wyolet/symbol/adapters/go_ast/daemon/`` to
enable these tests locally; CI builds the binary in a setup step.
"""

import shutil
from pathlib import Path

import pytest

from wyolet.symbol.adapters.go_ast import GoAstAdapter
from wyolet.symbol.protocols.types import FileScan

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "go_project"
FIXTURE_FILE = FIXTURE_DIR / "user.go"


def _skip_if_no_daemon() -> GoAstAdapter:
    adapter = GoAstAdapter()
    if not adapter.is_enabled:
        pytest.skip(
            "go-scan binary not available — build with `go build .` in "
            "src/wyolet/symbol/adapters/go_ast/daemon/"
        )
    return adapter


# ── adapter primitives ────────────────────────────────────────────


def test_adapter_lang_and_enabled():
    adapter = _skip_if_no_daemon()
    assert adapter.lang == "go"
    assert adapter.is_enabled is True


def test_validate_syntax_good_source():
    adapter = _skip_if_no_daemon()
    result = adapter.validate_syntax(FIXTURE_FILE.read_bytes())
    assert result.ok is True
    assert result.error_message is None


def test_validate_syntax_broken_source():
    adapter = _skip_if_no_daemon()
    result = adapter.validate_syntax(b"package x\nfunc broken( {}\n")
    assert result.ok is False
    assert result.error_line is not None
    assert result.error_line >= 1


def test_signature_func_via_adapter():
    adapter = _skip_if_no_daemon()
    sig = adapter.signature("func New(name string) *User {\n\treturn nil\n}")
    # gopls/godoc convention: declaration WITHOUT the body-opening brace.
    assert not sig.endswith("{")
    assert sig == "func New(name string) *User"


def test_signature_method_via_adapter():
    adapter = _skip_if_no_daemon()
    sig = adapter.signature(
        "func (u *User) Greet() string {\n\treturn \"\"\n}"
    )
    assert sig == "func (u *User) Greet() string"


def test_module_prefix_uses_go_mod(tmp_path: Path):
    """When a go.mod exists, module_prefix returns the declared module path
    plus the directory's relative position under that go.mod.
    """
    adapter = _skip_if_no_daemon()
    (tmp_path / "go.mod").write_text("module github.com/wyolet/example\n\ngo 1.22\n")
    pkg_dir = tmp_path / "pkg" / "user"
    pkg_dir.mkdir(parents=True)
    file_path = pkg_dir / "user.go"
    file_path.write_text("package user\n")
    assert adapter.module_prefix(file_path, tmp_path) == (
        "github.com/wyolet/example/pkg/user"
    )


def test_module_prefix_root_of_module(tmp_path: Path):
    adapter = _skip_if_no_daemon()
    (tmp_path / "go.mod").write_text("module example.com/cli\n")
    file_path = tmp_path / "main.go"
    file_path.write_text("package main\n")
    assert adapter.module_prefix(file_path, tmp_path) == "example.com/cli"


def test_module_prefix_no_go_mod_falls_back_to_dir(tmp_path: Path):
    adapter = _skip_if_no_daemon()
    pkg_dir = tmp_path / "sub" / "pkg"
    pkg_dir.mkdir(parents=True)
    file_path = pkg_dir / "f.go"
    file_path.write_text("package pkg\n")
    # No go.mod anywhere — adapter falls back to directory relative to
    # project_root (not garbage).
    assert adapter.module_prefix(file_path, tmp_path) == "sub/pkg"


# ── scan_file end-to-end ──────────────────────────────────────────


def test_scan_file_returns_filescan():
    adapter = _skip_if_no_daemon()
    scan = adapter.scan_file(FIXTURE_FILE, FIXTURE_FILE.read_bytes(), module_prefix="ex/user")
    assert isinstance(scan, FileScan)
    assert scan.language == "go"
    assert scan.ok is True


def test_scan_file_finds_all_top_level_symbols():
    adapter = _skip_if_no_daemon()
    scan = adapter.scan_file(FIXTURE_FILE, FIXTURE_FILE.read_bytes(), module_prefix="ex/user")
    by_name = {s.name: s for s in scan.symbols}
    assert set(by_name) == {"MaxRetries", "DefaultName", "User", "Greet", "New"}
    assert by_name["MaxRetries"].kind == "const"
    assert by_name["DefaultName"].kind == "var"
    assert by_name["User"].kind == "type"
    assert by_name["Greet"].kind == "method"
    assert by_name["New"].kind == "function"


def test_scan_file_qualifies_method_with_receiver():
    adapter = _skip_if_no_daemon()
    scan = adapter.scan_file(FIXTURE_FILE, FIXTURE_FILE.read_bytes(), module_prefix="ex/user")
    greet = next(s for s in scan.symbols if s.name == "Greet")
    assert greet.qualified_path == "ex/user.User.Greet"


def test_scan_file_extracts_imports():
    adapter = _skip_if_no_daemon()
    scan = adapter.scan_file(FIXTURE_FILE, FIXTURE_FILE.read_bytes())
    sources = {imp.source for imp in scan.imports}
    assert sources == {"fmt", "strings"}


def test_scan_file_refs_classify_selector_tails_as_attr():
    """``strings.ToUpper`` should record ``strings`` as name and ``ToUpper``
    as attr — never as both. Regression net for the selector-tail bug.
    """
    adapter = _skip_if_no_daemon()
    scan = adapter.scan_file(FIXTURE_FILE, FIXTURE_FILE.read_bytes())
    greet = next(s for s in scan.symbols if s.name == "Greet")
    pairs = {(r.name, r.kind) for r in greet.refs}
    assert ("strings", "name") in pairs
    assert ("ToUpper", "attr") in pairs
    assert ("ToUpper", "name") not in pairs
    assert ("fmt", "name") in pairs
    assert ("Println", "attr") in pairs
    assert ("Println", "name") not in pairs


# ── integration with SymbolIndex ──────────────────────────────────


def test_symbol_index_builds_for_go_project(tmp_path: Path):
    _skip_if_no_daemon()
    shutil.copy(FIXTURE_FILE, tmp_path / "user.go")

    from wyolet.symbol.shared.context import build_context
    from wyolet.symbol.shared.symbol_index import SymbolIndex

    ctx = build_context(tmp_path)
    idx = SymbolIndex(ctx.cache)
    idx.build()

    assert idx.num_symbols() == 5
    kinds_by_name = {
        idx.path_of(i).rsplit(".", 1)[-1] if "." in idx.path_of(i) else idx.path_of(i):
        idx.kind_of(i)
        for i in range(idx.num_symbols())
    }
    # Last segment of qualified path = symbol name. With empty module_prefix
    # the qualified paths are bare names.
    assert kinds_by_name["MaxRetries"] == "const"
    assert kinds_by_name["Greet"] == "method"
    assert kinds_by_name["New"] == "function"

    # Every row tagged with the Go language id.
    for i in range(idx.num_symbols()):
        assert idx.language_of(i) == "go"


def test_symbol_index_callers_find_go_refs(tmp_path: Path):
    _skip_if_no_daemon()
    shutil.copy(FIXTURE_FILE, tmp_path / "user.go")

    from wyolet.symbol.shared.context import build_context
    from wyolet.symbol.shared.symbol_index import SymbolIndex

    ctx = build_context(tmp_path)
    idx = SymbolIndex(ctx.cache)
    idx.build()

    callers = idx.callers_of("Println")
    assert len(callers) >= 1
    # The caller should be inside Greet; resolve src_row → file/range.
    src_row, line, _kind = callers[0]
    assert idx.file_of(src_row).endswith("user.go")
    assert line >= 17  # body of Greet starts around line 17
