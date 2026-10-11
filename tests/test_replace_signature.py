"""Tests for parameter-rename propagation through `replace-symbol`.

A parameter rename is a CONSEQUENCE of a signature edit, not a standalone
verb. When `replace-symbol`'s new content renames a parameter (arity equal,
unambiguous positional pairing), the engine propagates that rename to every
keyword-argument call site (`f(param=...)` → `f(new=...)`) in the SAME
transaction — the def slot + body come from the agent's replacement text.

Covered:
  - kwarg propagation, same file + cross-file via import
  - method receiver discrimination with a decoy class
  - arity change → signature_changed, kwargs untouched
  - swap (a,b)->(b,a) → no propagation
  - multiple unambiguous renames in one replace
  - positional-only call sites untouched
  - unresolved factory-callee reported inside parameter_renames
"""

import subprocess
from pathlib import Path

from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.replace_symbol import (
    ReplaceSymbolRequest,
    ReplaceSymbolResult,
    apply_replace_symbol,
    resolve_replace_symbol,
)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _do(project: Path, qpath: str, content: str, *, dry_run: bool = False):
    idx, _ = get_or_build_index(project)
    req = resolve_replace_symbol(idx, qpath, content, project)
    if isinstance(req, ReplaceSymbolResult):
        return req  # error surfaced during resolve
    assert isinstance(req, ReplaceSymbolRequest)
    return apply_replace_symbol(req, project_root=project, dry_run=dry_run)


# ── kwarg propagation: same file + cross-file import ─────────────────


def test_param_rename_propagates_kwargs_same_and_cross_file(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(data, mode):\n"
        "    return (data, mode)\n"
        "save(data=1, mode='w')\n"      # kwarg — data rewritten
        "save(2, 'w')\n"                # positional — untouched
    )
    (tmp_path / "caller.py").write_text(
        "from m import save\n"
        "def go():\n"
        "    return save(data=2, mode='r')\n"
    )
    _git_init(tmp_path)

    result = _do(
        tmp_path, "m.save",
        "def save(payload, mode):\n    return (payload, mode)\n",
    )
    assert result.status == "applied", result.message
    m = (tmp_path / "m.py").read_text()
    assert "def save(payload, mode):" in m
    assert "save(payload=1, mode='w')" in m       # kwarg rewritten
    assert "save(2, 'w')" in m                     # positional untouched
    caller = (tmp_path / "caller.py").read_text()
    assert "save(payload=2, mode='r')" in caller   # cross-file kwarg rewritten
    assert "mode='r'" in caller                    # other kwarg untouched

    pr = {p.old: p for p in result.parameter_renames}
    assert "data" in pr and pr["data"].new == "payload"
    assert pr["data"].refs_updated == 2            # one per file
    assert not result.signature_changed


# ── method receiver discrimination with a decoy class ────────────────


def test_method_param_rename_receiver_discrimination(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self, data):\n"
        "        return data\n"
        "class B:\n"
        "    def save(self, data):\n"
        "        return data\n"
        "def use(a: A, b: B):\n"
        "    a.save(data=1)\n"          # A.save — rewrite
        "    b.save(data=2)\n"          # B.save (decoy) — untouched
    )
    _git_init(tmp_path)

    result = _do(
        tmp_path, "m.A.save",
        "def save(self, payload):\n    return payload\n",
    )
    assert result.status == "applied", result.message
    text = (tmp_path / "m.py").read_text()
    assert "class A:\n    def save(self, payload):" in text
    assert "class B:\n    def save(self, data):" in text   # decoy decl untouched
    assert "a.save(payload=1)" in text
    assert "b.save(data=2)" in text                        # decoy call untouched

    pr = {p.old: p for p in result.parameter_renames}
    assert pr["data"].refs_updated == 1
    assert pr["data"].skipped_mismatch == 1                # b.save(data=) skipped


# ── arity change → signature_changed, kwargs untouched ───────────────


def test_arity_change_sets_signature_changed_no_propagation(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(data, mode):\n"
        "    return (data, mode)\n"
        "save(data=1, mode='w')\n"
    )
    _git_init(tmp_path)

    result = _do(
        tmp_path, "m.save",
        "def save(data, mode, extra):\n    return (data, mode, extra)\n",
    )
    assert result.status == "applied", result.message
    assert result.signature_changed is True
    assert result.parameter_renames == ()
    # call site keyword untouched — we declined to guess the mapping
    assert "save(data=1, mode='w')" in (tmp_path / "m.py").read_text()


# ── swap (a,b)->(b,a) → no propagation ───────────────────────────────


def test_swap_reorder_no_propagation(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(a, b):\n"
        "    return (a, b)\n"
        "f(a=1, b=2)\n"
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.f", "def f(b, a):\n    return (b, a)\n")
    assert result.status == "applied", result.message
    # a swap is ambiguous under the conservative rule → no kwarg rewrites
    assert result.parameter_renames == ()
    assert result.signature_changed is True
    assert "f(a=1, b=2)" in (tmp_path / "m.py").read_text()


# ── multiple unambiguous renames in one replace ──────────────────────


def test_multiple_unambiguous_renames(tmp_path):
    (tmp_path / "m.py").write_text(
        "def g(alpha, beta):\n"
        "    return (alpha, beta)\n"
        "g(alpha=1, beta=2)\n"
    )
    _git_init(tmp_path)

    result = _do(
        tmp_path, "m.g",
        "def g(first, second):\n    return (first, second)\n",
    )
    assert result.status == "applied", result.message
    assert "g(first=1, second=2)" in (tmp_path / "m.py").read_text()
    pr = {p.old: p.new for p in result.parameter_renames}
    assert pr == {"alpha": "first", "beta": "second"}
    assert not result.signature_changed


# ── positional-only call sites untouched ─────────────────────────────


def test_positional_only_call_sites_untouched(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save(data):\n"
        "    return data\n"
        "save(1)\n"                     # positional — untouched
        "save(2)\n"                     # positional — untouched
    )
    _git_init(tmp_path)

    result = _do(tmp_path, "m.save", "def save(payload):\n    return payload\n")
    assert result.status == "applied", result.message
    m = (tmp_path / "m.py").read_text()
    assert "def save(payload):" in m
    assert "save(1)" in m and "save(2)" in m
    pr = {p.old: p for p in result.parameter_renames}
    assert pr["data"].refs_updated == 0            # no kwarg sites at all


# ── unresolved factory callee reported inside parameter_renames ──────


def test_unresolved_factory_callee_reported(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self, data):\n"
        "        return data\n"
        "def make():\n"
        "    return A()\n"
        "make().save(data=1)\n"         # receiver is a call — unresolvable
    )
    _git_init(tmp_path)

    result = _do(
        tmp_path, "m.A.save",
        "def save(self, payload):\n    return payload\n",
    )
    assert result.status == "applied", result.message
    # the def slot came from the replacement content; the factory kwarg is
    # unresolved and therefore left untouched.
    assert "def save(self, payload):" in (tmp_path / "m.py").read_text()
    assert "make().save(data=1)" in (tmp_path / "m.py").read_text()
    pr = {p.old: p for p in result.parameter_renames}
    assert pr["data"].unresolved == 1
    assert pr["data"].refs_updated == 0
