"""Tier-2 static-MRO rename tests: inheritance membership + super().

These exercise the careful-mode path in `_python_ast_rename.rename_member`
where a rename target's owner class is matched against a receiver's
*inclusive ancestor set* (the class plus its transitive `class_bases`),
plus static `super()` receiver resolution.

Each case deliberately includes a same-leaf decoy so that
`owners_of_leaf(leaf) > 1` and the fast path (which rewrites everything
unconditionally) does NOT engage — otherwise the MRO logic is untested.
"""

import subprocess
from pathlib import Path

from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.rename_symbol import (
    RenameSymbolRequest,
    apply_rename_symbol,
    resolve_rename_symbol,
)


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


def _write(tmp_path: Path, source: str) -> Path:
    p = tmp_path / "m.py"
    p.write_text(source)
    _git_init(tmp_path)
    return p


# ── a. single inheritance ───────────────────────────────────────────


def test_single_inheritance_rewrites_subclass_call(tmp_path):
    """Sub inherits Base.ping; a Sub-typed receiver's ping() must rewrite.
    Before tier-2 this was wrongly skipped (equality-only match)."""
    p = _write(
        tmp_path,
        "class Base:\n"
        "    def ping(self): return 1\n"
        "class Sub(Base):\n"
        "    pass\n"
        "class Other:\n"                       # decoy → careful mode
        "    def ping(self): return 2\n"
        "s = Sub()\n"
        "s.ping()\n"
    )
    result = _do_rename(tmp_path, "m.Base.ping", "pong")
    assert result.status == "applied"
    text = p.read_text()
    assert "class Base:\n    def pong(" in text
    assert "s.pong()" in text                   # subclass call rewritten
    assert "class Other:\n    def ping(" in text  # decoy untouched
    assert not result.unresolved


# ── b. 3-deep chain ─────────────────────────────────────────────────


def test_three_deep_chain_rewrites(tmp_path):
    p = _write(
        tmp_path,
        "class A:\n"
        "    def method(self): return 1\n"
        "class B(A):\n"
        "    pass\n"
        "class C(B):\n"
        "    pass\n"
        "class Other:\n"                        # decoy
        "    def method(self): return 2\n"
        "def use(x: C):\n"
        "    x.method()\n"
    )
    result = _do_rename(tmp_path, "m.A.method", "renamed")
    text = p.read_text()
    assert "class A:\n    def renamed(" in text
    assert "def use(x: C):\n    x.renamed()" in text
    assert "class Other:\n    def method(" in text
    assert not result.unresolved


# ── c. diamond ──────────────────────────────────────────────────────


def test_diamond_rewrites_no_infinite_loop(tmp_path):
    p = _write(
        tmp_path,
        "class A:\n"
        "    def m(self): return 1\n"
        "class B(A):\n"
        "    pass\n"
        "class C(A):\n"
        "    pass\n"
        "class D(B, C):\n"
        "    pass\n"
        "class Other:\n"                        # decoy
        "    def m(self): return 2\n"
        "def use(x: D):\n"
        "    x.m()\n"
    )
    result = _do_rename(tmp_path, "m.A.m", "renamed")
    text = p.read_text()
    assert "class A:\n    def renamed(" in text
    assert "def use(x: D):\n    x.renamed()" in text
    assert "class Other:\n    def m(" in text
    assert not result.unresolved


# ── d. unrelated class, same leaf → skipped_mismatch ────────────────


def test_unrelated_class_same_leaf_skipped(tmp_path):
    p = _write(
        tmp_path,
        "class Base:\n"
        "    def ping(self): return 1\n"
        "class Unrelated:\n"
        "    def ping(self): return 2\n"
        "b = Unrelated()\n"
        "b.ping()\n"
    )
    result = _do_rename(tmp_path, "m.Base.ping", "pong")
    text = p.read_text()
    assert "class Base:\n    def pong(" in text
    assert "b.ping()" in text                   # decoy call intact
    assert "class Unrelated:\n    def ping(" in text
    assert any(
        s.resolved_to_qpath == "m.Unrelated" for s in result.skipped_mismatch
    ), f"expected m.Unrelated skip; got {result.skipped_mismatch}"


# ── e. external (unresolvable) base → skipped, not rewrite/unresolved ─


def test_external_base_receiver_skipped(tmp_path):
    """Sub inherits an unresolvable third-party base. Renaming a DIFFERENT
    local class's same-leaf method must NOT rewrite the Sub-typed call and
    must NOT surface it as unresolved — it's a clean mismatch."""
    p = _write(
        tmp_path,
        "class Local:\n"
        "    def handle(self): return 1\n"
        "class Decoy:\n"                        # forces careful mode
        "    def handle(self): return 2\n"
        "class Sub(SomeThirdPartyBase):\n"
        "    pass\n"
        "s = Sub()\n"
        "s.handle()\n"
    )
    result = _do_rename(tmp_path, "m.Local.handle", "process")
    text = p.read_text()
    assert "class Local:\n    def process(" in text
    assert "s.handle()" in text                 # not rewritten
    assert any(
        s.resolved_to_qpath == "m.Sub" for s in result.skipped_mismatch
    ), f"expected m.Sub skip; got {result.skipped_mismatch}"
    assert not any(
        u.receiver_source == "s" for u in result.unresolved
    ), f"s.handle() must not be unresolved; got {result.unresolved}"


# ── f. super() ──────────────────────────────────────────────────────


def test_super_rewrites_when_renaming_base_method(tmp_path):
    p = _write(
        tmp_path,
        "class Base:\n"
        "    def ping(self): return 1\n"
        "class Sub(Base):\n"
        "    def ping(self): return super().ping()\n"
    )
    result = _do_rename(tmp_path, "m.Base.ping", "pong")
    text = p.read_text()
    assert "class Base:\n    def pong(" in text          # base decl
    assert "return super().pong()" in text               # super() site rewritten
    assert "class Sub(Base):\n    def ping(" in text      # Sub's own def untouched
    assert not result.unresolved


def test_super_not_rewritten_when_renaming_subclass_method(tmp_path):
    p = _write(
        tmp_path,
        "class Base:\n"
        "    def ping(self): return 1\n"
        "class Sub(Base):\n"
        "    def ping(self): return super().ping()\n"
    )
    result = _do_rename(tmp_path, "m.Sub.ping", "pong")
    text = p.read_text()
    assert "class Sub(Base):\n    def pong(" in text      # sub decl renamed
    assert "return super().ping()" in text               # super() site NOT touched
    assert "class Base:\n    def ping(" in text           # base untouched
    assert any(
        "super()" in s.resolved_to_qpath for s in result.skipped_mismatch
    ), f"expected super() skip; got {result.skipped_mismatch}"


# ── g. cycle guard ──────────────────────────────────────────────────


def test_cycle_guard_no_hang(tmp_path):
    """Hand-written illegal cycle A(B), B(A): static traversal must not hang."""
    p = _write(
        tmp_path,
        "class A(B):\n"
        "    def m(self): return 1\n"
        "class B(A):\n"
        "    def other(self): return 2\n"
        "class Decoy:\n"                        # forces careful mode
        "    def m(self): return 3\n"
        "def use(x: A):\n"
        "    x.m()\n"
    )
    result = _do_rename(tmp_path, "m.A.m", "renamed")
    text = p.read_text()
    assert "class A(B):\n    def renamed(" in text
    assert "def use(x: A):\n    x.renamed()" in text
    assert not result.unresolved
