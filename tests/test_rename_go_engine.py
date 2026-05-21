"""Tests for the Go AST-based rename engine.

Closes the Go side of issue #15: cross-type method discrimination
using go/types via golang.org/x/tools/go/packages. Tier-2 correctness
(no AST-only inference gaps) — receiver types resolve exactly.

Skipped when the go-scan binary isn't built. Pattern matches
tests/test_mcp_go_integration.py.
"""

import subprocess
from pathlib import Path

import pytest

from wyolet.symbol.adapters.go_ast import GoAstAdapter
from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.rename_symbol import (
    RenameSymbolRequest,
    apply_rename_symbol,
    resolve_rename_symbol,
)


def _skip_if_no_daemon() -> None:
    if not GoAstAdapter().is_enabled:
        pytest.skip("go-scan binary not built; see tests/test_go_adapter.py for setup")


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _do_rename(project: Path, qpath: str, new_name: str, *, dry_run: bool = False):
    idx, _ = get_or_build_index(project)
    req = resolve_rename_symbol(idx, qpath, new_name, project)
    assert isinstance(req, RenameSymbolRequest), f"resolve failed: {req}"
    return apply_rename_symbol(req, project_root=project, dry_run=dry_run, _index=idx)


def _go_build_ok(project: Path) -> bool:
    """Verify the project still type-checks after a rename."""
    result = subprocess.run(
        ["go", "build", "./..."], cwd=project, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout, result.stderr)
    return result.returncode == 0


@pytest.fixture
def cross_type_project(tmp_path: Path) -> Path:
    """The original #15 reproducer: foo.Service.Save and bar.Service.Save."""
    _skip_if_no_daemon()
    (tmp_path / "go.mod").write_text("module example/test\n\ngo 1.22\n")
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "foo.go").write_text(
        "package foo\n\n"
        "type Service struct{}\n\n"
        'func (s *Service) Save() string { return "foo saved" }\n'
    )
    (tmp_path / "bar").mkdir()
    (tmp_path / "bar" / "bar.go").write_text(
        "package bar\n\n"
        "type Service struct{}\n\n"
        'func (s *Service) Save() string { return "bar saved" }\n'
    )
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "main.go").write_text(
        "package main\n\n"
        'import (\n    "fmt"\n    "example/test/bar"\n    "example/test/foo"\n)\n\n'
        "func main() {\n"
        "    f := &foo.Service{}\n"
        "    b := &bar.Service{}\n"
        "    fmt.Println(f.Save())\n"
        "    fmt.Println(b.Save())\n"
        "}\n"
    )
    _git_init(tmp_path)
    return tmp_path


# ── cross-type method discrimination (the #15 reproducer) ──────────


def test_method_cross_type_discrimination(cross_type_project: Path):
    """Renaming foo.Service.Save must NOT rewrite bar.Service.Save callers."""
    result = _do_rename(cross_type_project, "example/test/foo.Service.Save", "Store")
    assert result.status == "applied"

    foo = (cross_type_project / "foo" / "foo.go").read_text()
    bar = (cross_type_project / "bar" / "bar.go").read_text()
    main = (cross_type_project / "cmd" / "main.go").read_text()

    assert "func (s *Service) Store()" in foo
    assert "func (s *Service) Save()" in bar         # untouched
    assert "f.Store()" in main                        # rewritten
    assert "b.Save()" in main                         # untouched

    # skipped_mismatch should surface b.Save() resolving to bar.Service.Save
    assert any(
        s.resolved_to_qpath == "example/test/bar.Service.Save"
        for s in result.skipped_mismatch
    ), f"expected skipped_mismatch for bar.Service.Save; got {result.skipped_mismatch}"

    assert _go_build_ok(cross_type_project), "project must still build after rename"


def test_method_rename_dry_run_does_not_write(cross_type_project: Path):
    result = _do_rename(cross_type_project, "example/test/foo.Service.Save", "Store", dry_run=True)
    assert result.status == "dry_run"
    foo = (cross_type_project / "foo" / "foo.go").read_text()
    assert "func (s *Service) Save()" in foo   # not yet rewritten


# ── function cross-package discrimination ──────────────────────────


def test_function_cross_package_discrimination(tmp_path: Path):
    _skip_if_no_daemon()
    (tmp_path / "go.mod").write_text("module example/test\n\ngo 1.22\n")
    for pkg in ("foo", "bar"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / f"{pkg}.go").write_text(
            f"package {pkg}\n\nfunc Make() string {{ return \"{pkg}\" }}\n"
        )
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "main.go").write_text(
        "package main\n\n"
        'import (\n    "fmt"\n    "example/test/bar"\n    "example/test/foo"\n)\n\n'
        "func main() {\n"
        "    fmt.Println(foo.Make())\n"
        "    fmt.Println(bar.Make())\n"
        "}\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "example/test/foo.Make", "Construct")
    assert result.status == "applied"

    foo = (tmp_path / "foo" / "foo.go").read_text()
    bar = (tmp_path / "bar" / "bar.go").read_text()
    main = (tmp_path / "cmd" / "main.go").read_text()

    assert "func Construct() string" in foo
    assert "func Make() string" in bar            # untouched
    assert "foo.Construct()" in main              # rewritten
    assert "bar.Make()" in main                   # untouched
    assert _go_build_ok(tmp_path)


# ── type rename + receiver type updates ────────────────────────────


def test_type_rename_updates_receivers_and_instantiations(cross_type_project: Path):
    """Renaming foo.Service updates type decl, method receivers, and
    foo.Service{} instantiations — but leaves bar.Service alone."""
    result = _do_rename(cross_type_project, "example/test/foo.Service", "Worker")
    assert result.status == "applied"

    foo = (cross_type_project / "foo" / "foo.go").read_text()
    bar = (cross_type_project / "bar" / "bar.go").read_text()
    main = (cross_type_project / "cmd" / "main.go").read_text()

    assert "type Worker struct" in foo
    assert "func (s *Worker) Save()" in foo       # receiver type updated
    assert "type Service struct" in bar           # bar untouched
    assert "func (s *Service) Save()" in bar
    assert "&foo.Worker{}" in main                # instantiation updated
    assert "&bar.Service{}" in main               # different pkg untouched

    assert _go_build_ok(cross_type_project)


# ── interface dispatch: discriminator correctly identifies interface ──


def test_interface_call_resolves_to_interface_not_implementer(tmp_path: Path):
    """Interface-typed `s.Save()` where s is `Saver` must resolve to the
    interface method, not to a concrete implementer. Renaming a single
    implementer doesn't rewrite the interface call site. (This leaves the
    interface contract broken — gopls-style implementation-set rename is
    a separate concern; we just verify the discriminator is honest.)"""
    _skip_if_no_daemon()
    (tmp_path / "go.mod").write_text("module example/iface\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text(
        "package main\n\n"
        'import "fmt"\n\n'
        "type Saver interface { Save() string }\n\n"
        "type A struct{}\n"
        'func (A) Save() string { return "a" }\n\n'
        "type B struct{}\n"
        'func (B) Save() string { return "b" }\n\n'
        "func useIface(s Saver) string { return s.Save() }\n\n"
        "func main() {\n"
        "    a := A{}\n"
        "    fmt.Println(a.Save())  // direct A receiver\n"
        "    b := B{}\n"
        "    fmt.Println(b.Save())  // direct B receiver — must NOT change\n"
        "    fmt.Println(useIface(a))\n"
        "}\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "example/iface.A.Save", "Store")
    assert result.status == "applied"
    text = (tmp_path / "main.go").read_text()

    assert "func (A) Store()" in text
    assert "func (B) Save()" in text              # B untouched
    assert "a.Store()" in text                    # direct A call rewritten
    assert "b.Save()" in text                     # direct B call untouched
    assert "return s.Save()" in text              # interface call untouched

    # Both the interface method and B.Save show up in skipped_mismatch
    # — that's the discriminator working: those refs really do resolve
    # to different declarations than A.Save.
    qpaths = {s.resolved_to_qpath for s in result.skipped_mismatch}
    assert "example/iface.Saver.Save" in qpaths
    assert "example/iface.B.Save" in qpaths

    # The interface contract impact is surfaced separately — the agent
    # sees that A implements Saver and the rename leaves it unsatisfied.
    affected_qpaths = {a.interface_qpath for a in result.affected_interfaces}
    assert "example/iface.Saver" in affected_qpaths, (
        f"expected Saver in affected_interfaces; got {result.affected_interfaces}"
    )


# ── factory-returned receiver (tier-2 win over Python tier-1) ──────


def test_factory_return_receiver_resolved(tmp_path: Path):
    """Python tier-1 documented `y := make_a(); y.save()` as unresolved.
    Go tier-2 with go/types handles it exactly — function-return types
    are part of the type info."""
    _skip_if_no_daemon()
    (tmp_path / "go.mod").write_text("module example/factory\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text(
        "package main\n\n"
        "type A struct{}\n"
        'func (A) Save() string { return "a" }\n\n'
        "type B struct{}\n"
        'func (B) Save() string { return "b" }\n\n'
        "func makeA() A { return A{} }\n"
        "func makeB() B { return B{} }\n\n"
        "func main() {\n"
        "    a := makeA()\n"
        "    a.Save()       // resolves to A.Save\n"
        "    b := makeB()\n"
        "    b.Save()       // resolves to B.Save\n"
        "}\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "example/factory.A.Save", "Store")
    assert result.status == "applied"
    text = (tmp_path / "main.go").read_text()

    assert "func (A) Store()" in text
    assert "func (B) Save()" in text              # B untouched
    assert "a.Store()" in text                    # factory-typed A.Save call rewritten
    assert "b.Save()" in text                     # factory-typed B.Save untouched
    # No unresolved sites — go/types resolves factory returns exactly.
    assert not result.unresolved, f"factory return should resolve cleanly via go/types; got {result.unresolved}"

    assert _go_build_ok(tmp_path)


# ── embedded methods (Go's equivalent of inheritance) ──────────────


def test_embedded_method_promotion_does_not_leak(tmp_path: Path):
    """A type B that embeds A inherits A.Save. Renaming A.Save updates
    the embedded definition; calls on a B value through the promoted
    method still resolve to A.Save and get renamed too."""
    _skip_if_no_daemon()
    (tmp_path / "go.mod").write_text("module example/embed\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text(
        "package main\n\n"
        "type A struct{}\n"
        'func (A) Save() string { return "a" }\n\n'
        "type B struct { A }\n\n"   # B embeds A, promotes Save
        "type C struct{}\n"
        'func (C) Save() string { return "c" }\n\n'  # different type, same name
        "func main() {\n"
        "    a := A{}\n"
        "    a.Save()\n"
        "    b := B{}\n"
        "    b.Save()       // promoted from A.Save\n"
        "    c := C{}\n"
        "    c.Save()       // C.Save — must NOT change\n"
        "}\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "example/embed.A.Save", "Store")
    assert result.status == "applied"
    text = (tmp_path / "main.go").read_text()

    assert "func (A) Store()" in text
    assert "func (C) Save()" in text              # C is a different declaration
    assert "a.Store()" in text
    assert "b.Store()" in text                    # promoted call rewrites too
    assert "c.Save()" in text                     # different type untouched

    assert _go_build_ok(tmp_path)
