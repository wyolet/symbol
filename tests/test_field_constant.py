"""WP3: constant + field scanner rows and their rename engines.

Two halves:
  - Scanner emission — module UPPER_CASE constants, class-body fields,
    __init__ `self.x` fields, dedupe, and the negatives (lowercase module
    assigns, locals, `self.x` outside __init__).
  - Rename end to end via SymbolRenamer — module constant (routes through
    the module-binding engine, import sites follow) and class field
    (receiver-aware: owner + subclass reads rewrite, decoy class skipped,
    class-body declaration rewritten).
"""

import subprocess
from pathlib import Path

from wyolet.symbol.adapters.python_ast import PythonAstAdapter
from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.rename_symbol import (
    RenameSymbolRequest,
    apply_rename_symbol,
    resolve_rename_symbol,
)


# ── helpers ─────────────────────────────────────────────────────────


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


def _scan_kinds(source: str) -> dict[str, str]:
    """qpath -> kind for every scanned symbol (recursively), module prefix 'm'."""
    adapter = PythonAstAdapter()
    scan = adapter.scan_file(Path("m.py"), source.encode(), module_prefix="m")
    out: dict[str, str] = {}

    def walk(syms):
        for s in syms:
            out[s.qualified_path] = s.kind
            walk(s.children)

    walk(scan.symbols)
    return out


# ── scanner: constants ──────────────────────────────────────────────


def test_scanner_emits_upper_case_constants_only():
    kinds = _scan_kinds(
        "COLOR = 'red'\n"
        "MAX_SIZE: int = 10\n"
        "lower = 1\n"
        "MixedCase = 2\n"
    )
    assert kinds.get("m.COLOR") == "constant"
    assert kinds.get("m.MAX_SIZE") == "constant"      # annotated constant
    assert "m.lower" not in kinds                      # lowercase → not a constant
    assert "m.MixedCase" not in kinds                  # mixed case → not a constant


def test_scanner_constant_only_at_module_level():
    """A local UPPER_CASE assignment inside a function is not a constant."""
    kinds = _scan_kinds(
        "def f():\n"
        "    LOCAL = 1\n"
        "    return LOCAL\n"
    )
    assert "m.f.LOCAL" not in kinds
    assert "m.f" in kinds


# ── scanner: fields ─────────────────────────────────────────────────


def test_scanner_emits_class_body_and_init_fields():
    kinds = _scan_kinds(
        "class C:\n"
        "    field_a: int = 0\n"
        "    field_b = 'x'\n"
        "    def __init__(self):\n"
        "        self.count = 0\n"
        "        local_var = 3\n"
        "        return local_var\n"
        "    def method(self):\n"
        "        self.late = 9\n"
    )
    assert kinds.get("m.C.field_a") == "field"       # class-body annotated
    assert kinds.get("m.C.field_b") == "field"       # class-body plain
    assert kinds.get("m.C.count") == "field"         # __init__ self.count
    assert "m.C.local_var" not in kinds              # __init__ local, not a field
    assert "m.C.late" not in kinds                   # self.x outside __init__ → skip
    assert kinds.get("m.C.method") == "method"       # methods still methods


def test_scanner_field_dedupe_classbody_wins():
    """A name declared both in the class body and __init__ yields ONE row,
    ranged on the class-body declaration."""
    src = (
        "class C:\n"
        "    count: int = 0\n"
        "    def __init__(self):\n"
        "        self.count = 5\n"
    )
    adapter = PythonAstAdapter()
    scan = adapter.scan_file(Path("m.py"), src.encode(), module_prefix="m")
    fields = [
        c for cls in scan.symbols for c in cls.children if c.kind == "field"
    ]
    assert [f.qualified_path for f in fields] == ["m.C.count"]
    # Ranged on the class-body line (line 2), not the __init__ assignment.
    assert fields[0].line_range[0] == 2


def test_scanner_no_fields_for_module_or_locals():
    kinds = _scan_kinds(
        "x = 1\n"
        "def f():\n"
        "    y = 2\n"
        "    return y\n"
    )
    assert all(k != "field" for k in kinds.values())


# ── rename: module constant (+ cross-file import) ───────────────────


def test_rename_module_constant_updates_import_sites(tmp_path):
    (tmp_path / "consts.py").write_text("MAX = 10\n")
    (tmp_path / "user.py").write_text(
        "from consts import MAX\n"
        "def area():\n"
        "    return MAX * 2\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "consts.MAX", "LIMIT")
    assert result.status == "applied", result.message

    assert (tmp_path / "consts.py").read_text() == "LIMIT = 10\n"
    user = (tmp_path / "user.py").read_text()
    assert "from consts import LIMIT" in user       # import rewritten
    assert "return LIMIT * 2" in user               # usage rewritten


# ── rename: class field (receiver-aware) ────────────────────────────


def test_rename_field_owner_and_subclass_reads(tmp_path):
    """Renaming Base.count rewrites the class-body decl, self.count in the
    owner, and self.count reads in a subclass method — while a same-named
    field on an unrelated class is left alone (careful mode via the decoy)."""
    p = tmp_path / "m.py"
    p.write_text(
        "class Base:\n"
        "    count: int = 0\n"
        "    def inc(self):\n"
        "        self.count += 1\n"
        "class Sub(Base):\n"
        "    def read(self):\n"
        "        return self.count\n"
        "class Other:\n"                     # decoy → owners_of_leaf > 1 → careful mode
        "    count = 99\n"
        "    def read(self):\n"
        "        return self.count\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "m.Base.count", "total")
    assert result.status == "applied", result.message
    text = p.read_text()

    assert "    total: int = 0\n" in text            # class-body decl rewritten
    assert "self.total += 1" in text                 # owner write rewritten
    assert "return self.total" in text               # subclass read rewritten
    assert "    count = 99\n" in text                # decoy class-body untouched
    # Other.read's self.count resolves to m.Other → skipped, not rewritten.
    assert any(
        s.resolved_to_qpath == "m.Other" for s in result.skipped_mismatch
    ), f"expected m.Other skip; got {result.skipped_mismatch}"
    # Exactly one self.count survives (Other.read), never rewritten.
    assert text.count("self.count") == 1
