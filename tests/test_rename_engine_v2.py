"""Tests for the v2 AST-based rename engine.

Covers behaviors the old regex path did not have:
  - Cross-type method discrimination (issue #15)
  - Receiver-type resolution: self/cls, param annotations, assignments
  - Module-binding shadowing detection
  - Local-import-of-target recognition (not a shadow)
  - Parent-package re-export of imports
"""

import subprocess
from pathlib import Path

import pytest

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


def _do_rename(project: Path, qpath: str, new_name: str, *, dry_run: bool = True):
    idx, _ = get_or_build_index(project)
    req = resolve_rename_symbol(idx, qpath, new_name, project)
    assert isinstance(req, RenameSymbolRequest), f"resolve failed: {req}"
    return apply_rename_symbol(req, project_root=project, dry_run=dry_run, _index=idx)


# ── cross-type method discrimination ────────────────────────────────


def test_method_collision_skips_other_class(tmp_path):
    """The original #15 reproducer: renaming A.save must not touch B.save."""
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self): return 1\n"
        "class B:\n"
        "    def save(self): return 2\n"
        "a = A(); b = B()\n"
        "a.save()\n"
        "b.save()\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "m.A.save", "persist", dry_run=False)
    assert result.status == "applied"

    text = (tmp_path / "m.py").read_text()
    assert "class A:\n    def persist(" in text
    assert "class B:\n    def save(" in text       # B.save untouched
    assert "a.persist()" in text                    # a.save → renamed
    assert "b.save()" in text                       # b.save → NOT renamed
    # Skipped sites tracked in the result for the agent to review
    assert any(
        s.line == 7 and s.resolved_to_qpath == "m.B"
        for s in result.skipped_mismatch
    )


def test_method_self_reference_resolves_to_enclosing_class(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self):\n"
        "        return self.save  # self → A → match\n"
        "class B:\n"
        "    def save(self): return 1\n"
    )
    _git_init(tmp_path)

    _do_rename(tmp_path, "m.A.save", "persist", dry_run=False)
    text = (tmp_path / "m.py").read_text()
    assert "return self.persist" in text
    assert "class B:\n    def save(" in text


def test_method_param_annotation_resolves_receiver(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self): return 1\n"
        "class B:\n"
        "    def save(self): return 2\n"
        "def use(x: A):\n"
        "    x.save()\n"
        "def use_b(x: B):\n"
        "    x.save()\n"
    )
    _git_init(tmp_path)

    _do_rename(tmp_path, "m.A.save", "persist", dry_run=False)
    text = (tmp_path / "m.py").read_text()
    assert "def use(x: A):\n    x.persist()" in text
    assert "def use_b(x: B):\n    x.save()" in text   # B's not touched


def test_method_factory_call_surfaces_as_unresolved(tmp_path):
    """Factory-returned receivers can't be resolved without type inference.
    Should be surfaced loudly (status=needs_review if it's the only ref)."""
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self): return 1\n"
        "class B:\n"
        "    def save(self): return 2\n"
        "def make(): return A()\n"
        "y = make()\n"
        "y.save()\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "m.A.save", "persist")
    # The declaration site rewrites; the y.save() call is unresolved.
    assert any(u.line == 7 and u.receiver_source == "y" for u in result.unresolved), \
        f"expected unresolved y.save site; got {result.unresolved}"
    assert "bound to `make()`" in result.unresolved[0].why


def test_method_fast_path_when_leaf_globally_unique(tmp_path):
    """No collision → fast path: rewrite every site without analysis cost."""
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def uniquely_named(self): return 1\n"
        "a = A()\n"
        "a.uniquely_named()\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "m.A.uniquely_named", "renamed", dry_run=False)
    assert result.status == "applied"
    assert result.refs_updated == 2  # declaration + call
    assert not result.unresolved
    assert not result.skipped_mismatch


# ── module-binding shadowing detection ──────────────────────────────


def test_module_function_local_assignment_shadow(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(): return 1\n"
        "def caller():\n"
        "    return save()        # module-level — rewrite\n"
        "def shadowed():\n"
        "    save = 42            # local shadow\n"
        "    return save + 1      # references shadow, NOT module save\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "m.save", "persist", dry_run=False)
    text = (tmp_path / "m.py").read_text()
    assert "def persist():" in text
    assert "return persist()" in text                  # module-level call rewritten
    assert "    save = 42" in text                     # local untouched
    assert "    return save + 1" in text               # shadow ref untouched
    assert any("shadowed" in (s.resolved_to_qpath or "") for s in result.skipped_mismatch)


def test_module_function_param_shadow(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(): return 1\n"
        "def caller(save):\n"
        "    return save()        # param shadow\n"
    )
    _git_init(tmp_path)

    _do_rename(tmp_path, "m.save", "persist", dry_run=False)
    text = (tmp_path / "m.py").read_text()
    assert "def persist(): return 1" in text
    assert "def caller(save):" in text
    assert "return save()" in text


def test_module_function_loop_var_shadow(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(): return 1\n"
        "def caller(items):\n"
        "    for save in items:\n"
        "        save += 1\n"
    )
    _git_init(tmp_path)

    _do_rename(tmp_path, "m.save", "persist", dry_run=False)
    text = (tmp_path / "m.py").read_text()
    assert "def persist():" in text
    assert "for save in items:" in text
    assert "save += 1" in text


def test_module_function_except_with_shadow(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(): return 1\n"
        "def use_except():\n"
        "    try: pass\n"
        "    except Exception as save:\n"
        "        return save\n"
        "def use_with():\n"
        "    with open('x') as save:\n"
        "        return save\n"
    )
    _git_init(tmp_path)

    _do_rename(tmp_path, "m.save", "persist", dry_run=False)
    text = (tmp_path / "m.py").read_text()
    assert "def persist():" in text
    assert "except Exception as save:" in text
    assert "with open('x') as save:" in text


def test_module_function_nested_def_shadow(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(): return 1\n"
        "def caller():\n"
        "    def save(): return 2  # nested def shadows\n"
        "    return save()         # refers to nested\n"
    )
    _git_init(tmp_path)

    _do_rename(tmp_path, "m.save", "persist", dry_run=False)
    text = (tmp_path / "m.py").read_text()
    assert "def persist(): return 1" in text
    assert "def save(): return 2" in text
    assert "return save()" in text


def test_module_function_local_import_of_target_still_rewrites(tmp_path):
    """`from m import save` inside a function body IS our target —
    not a shadow. Both the local import alias AND the reference
    must be rewritten."""
    (tmp_path / "m.py").write_text("def save(): return 1\n")
    (tmp_path / "caller.py").write_text(
        "def use():\n"
        "    from m import save  # local import — same target\n"
        "    return save()       # refers to imported save\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "m.save", "persist", dry_run=False)
    assert result.status == "applied"
    caller = (tmp_path / "caller.py").read_text()
    assert "from m import persist" in caller, caller
    assert "return persist()" in caller, caller


# ── re-export handling ─────────────────────────────────────────────


def test_parent_package_reexport_rewrites_import(tmp_path):
    """`from pkg import save` where pkg/__init__ re-exports from pkg.impl
    should rewrite when renaming pkg.impl.save."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from pkg.impl import save\n")
    (pkg / "impl.py").write_text("def save(): return 1\n")
    (tmp_path / "caller.py").write_text(
        "from pkg import save\n"
        "def use():\n"
        "    return save()\n"
    )
    _git_init(tmp_path)

    result = _do_rename(tmp_path, "pkg.impl.save", "persist", dry_run=False)
    assert result.status == "applied"
    caller = (tmp_path / "caller.py").read_text()
    init = (pkg / "__init__.py").read_text()
    impl = (pkg / "impl.py").read_text()
    assert "from pkg import persist" in caller
    assert "return persist()" in caller
    assert "from pkg.impl import persist" in init   # re-export site also rewrites
    assert "def persist():" in impl


# ── unrelated module-level binding ─────────────────────────────────


def test_unrelated_module_binding_not_rewritten(tmp_path):
    """If a different file defines its own module-level `save` (no import
    from our module), renaming m.save must not touch it."""
    (tmp_path / "m.py").write_text("def save(): return 1\n")
    (tmp_path / "other.py").write_text(
        "def save(): return 99  # unrelated, no import\n"
        "def use():\n"
        "    return save()\n"
    )
    _git_init(tmp_path)

    _do_rename(tmp_path, "m.save", "persist", dry_run=False)
    other = (tmp_path / "other.py").read_text()
    assert "def save(): return 99" in other
    assert "return save()" in other
