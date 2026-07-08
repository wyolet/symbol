"""Tests for `rename-parameter` — the signature-surface rename.

Covers:
  - declaring def slot + in-scope body uses; positional args untouched
  - keyword-argument call sites (same file + cross-file via import)
  - same-leaf call to a DIFFERENT function → skipped_mismatch
  - unresolvable callee whose keyword matches → unresolved
  - nested def rebinding the param → inner scope untouched
  - method parameter with receiver discrimination (two classes, same leaf)
  - collision with an existing parameter → error
"""

import subprocess
from pathlib import Path

from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.rename_parameter import (
    RenameParameterRequest,
    apply_rename_parameter,
    resolve_rename_parameter,
)
from wyolet.symbol.writes.rename_symbol import RenameSymbolResult


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _do(project: Path, func_qpath: str, param: str, new_name: str, *, dry_run: bool = False):
    idx, _ = get_or_build_index(project)
    req = resolve_rename_parameter(idx, func_qpath, param, new_name, project)
    if isinstance(req, RenameSymbolResult):
        return req  # error surfaced during resolve
    assert isinstance(req, RenameParameterRequest)
    return apply_rename_parameter(req, project_root=project, dry_run=dry_run, _index=idx)


# ── declaring def + body uses; positional untouched ──────────────────


def test_decl_and_body_uses_rewritten_positional_untouched(tmp_path):
    (tmp_path / "m.py").write_text(
        "def compute(count, step):\n"
        "    total = count + step\n"
        "    return total * count\n"
        "compute(3, 5)\n"           # positional — untouched
        "compute(count=3, step=5)\n"  # keyword — count rewritten
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.compute", "count", "n")
    assert result.status == "applied", result.message
    text = (tmp_path / "m.py").read_text()
    assert "def compute(n, step):" in text
    assert "total = n + step" in text
    assert "return total * n" in text
    assert "compute(3, 5)" in text          # positional call untouched
    assert "compute(count=3, step=5)".replace("count", "n") in text
    assert "compute(n=3, step=5)" in text
    assert "step" in text and "def compute(n, step)" in text  # step param untouched


# ── keyword call sites: same file + cross-file import ────────────────


def test_kwargs_rewritten_same_and_cross_file(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(data, mode):\n"
        "    return (data, mode)\n"
        "save(data=1, mode='w')\n"
    )
    (tmp_path / "caller.py").write_text(
        "from m import save\n"
        "def go():\n"
        "    return save(data=2, mode='r')\n"
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.save", "data", "payload")
    assert result.status == "applied", result.message
    assert "def save(payload, mode):" in (tmp_path / "m.py").read_text()
    assert "save(payload=1, mode='w')" in (tmp_path / "m.py").read_text()
    caller = (tmp_path / "caller.py").read_text()
    assert "save(payload=2, mode='r')" in caller
    assert "mode='r'" in caller  # other kwarg untouched


# ── same-leaf different function → skipped_mismatch ──────────────────


def test_same_leaf_different_function_skipped(tmp_path):
    (tmp_path / "m.py").write_text("def save(data):\n    return data\n")
    (tmp_path / "other.py").write_text(
        "def save(data):\n"          # unrelated same-leaf function
        "    return data\n"
        "def go():\n"
        "    return save(data=1)\n"  # resolves to other.save, NOT m.save
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.save", "data", "payload")
    assert "def save(payload):" in (tmp_path / "m.py").read_text()
    other = (tmp_path / "other.py").read_text()
    assert "def save(data):" in other        # other.save decl untouched
    assert "return save(data=1)" in other    # its call untouched
    assert any(
        s.file == "other.py" and s.resolved_to_qpath == "other.save"
        for s in result.skipped_mismatch
    ), result.skipped_mismatch


# ── unresolvable callee (method via factory) → unresolved ────────────


def test_unresolvable_callee_reports_unresolved(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self, data):\n"
        "        return data\n"
        "def make():\n"
        "    return A()\n"
        "make().save(data=1)\n"      # receiver is a call — unresolvable
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.A.save", "data", "payload")
    # declaration slot still rewrites; the factory kwarg is unresolved
    assert "def save(self, payload):" in (tmp_path / "m.py").read_text()
    assert any(
        u.line == 6 and "call expression" in u.why for u in result.unresolved
    ), result.unresolved
    # the unresolved site must NOT have been rewritten
    assert "make().save(data=1)" in (tmp_path / "m.py").read_text()


# ── nested def rebinding param → inner scope untouched ───────────────


def test_nested_def_rebinding_shadows(tmp_path):
    (tmp_path / "m.py").write_text(
        "def outer(value):\n"
        "    def inner(value):\n"       # nested param shadows
        "        return value + 1\n"    # refers to inner's value — untouched
        "    x = value                 # outer's value — rewritten\n"
        "    return inner(x)\n"
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.outer", "value", "seed")
    assert result.status == "applied", result.message
    text = (tmp_path / "m.py").read_text()
    assert "def outer(seed):" in text
    assert "def inner(value):" in text          # inner param untouched
    assert "return value + 1" in text           # inner body untouched
    assert "x = seed" in text                    # outer use rewritten


def test_nested_def_default_references_outer_param(tmp_path):
    (tmp_path / "m.py").write_text(
        "def outer(value):\n"
        "    def inner(x=value):\n"       # default evaluated in outer scope
        "        return x\n"
        "    return inner()\n"
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.outer", "value", "seed")
    assert result.status == "applied", result.message
    text = (tmp_path / "m.py").read_text()
    assert "def outer(seed):" in text
    assert "def inner(x=seed):" in text          # default rewritten (outer scope)


# ── method parameter: receiver discrimination ────────────────────────


def test_method_param_receiver_discrimination(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self, data):\n"
        "        return data\n"
        "class B:\n"
        "    def save(self, data):\n"
        "        return data\n"
        "def use(a: A, b: B):\n"
        "    a.save(data=1)\n"          # A.save — rewrite
        "    b.save(data=2)\n"          # B.save — untouched
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.A.save", "data", "payload")
    assert result.status == "applied", result.message
    text = (tmp_path / "m.py").read_text()
    assert "class A:\n    def save(self, payload):" in text
    assert "class B:\n    def save(self, data):" in text   # B decl untouched
    assert "a.save(payload=1)" in text
    assert "b.save(data=2)" in text                        # B call untouched
    assert any(
        s.line == 9 and s.resolved_to_qpath == "m.B" for s in result.skipped_mismatch
    ), result.skipped_mismatch


# ── collision with existing parameter → error ────────────────────────


def test_collision_with_existing_param_errors(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(a, b):\n"
        "    return a + b\n"
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.f", "a", "b")
    assert result.status == "error"
    assert result.error_code == "name_collision"
    assert (tmp_path / "m.py").read_text() == "def f(a, b):\n    return a + b\n"


def test_param_not_found_errors(tmp_path):
    (tmp_path / "m.py").write_text("def f(a):\n    return a\n")
    _git_init(tmp_path)

    result = _do(tmp_path, "m.f", "nope", "x")
    assert result.status == "error"
    assert result.error_code == "parameter_not_found"


def test_dry_run_does_not_write(tmp_path):
    (tmp_path / "m.py").write_text("def f(a):\n    return a\nf(a=1)\n")
    _git_init(tmp_path)

    before = (tmp_path / "m.py").read_text()
    result = _do(tmp_path, "m.f", "a", "x", dry_run=True)
    assert result.status == "dry_run"
    assert (tmp_path / "m.py").read_text() == before
