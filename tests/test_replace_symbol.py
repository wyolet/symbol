"""Tests for `symbol replace-symbol`."""

import subprocess
from pathlib import Path

import pytest

from wyolet.symbol.shared.symbol_index import SymbolIndex, get_or_build_index
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


def _index(project: Path) -> SymbolIndex:
    idx, _ = get_or_build_index(project)
    return idx


@pytest.fixture
def project(tmp_path):
    (tmp_path / "services.py").write_text(
        '''class UserService:
    def save(self, user):
        return user


class OrderService:
    def save(self, order):
        return order


def helper():
    return 42
'''
    )
    (tmp_path / "caller.py").write_text(
        '''from services import UserService, helper


def use():
    svc = UserService()
    return svc.save(helper())
'''
    )
    _git_init(tmp_path)
    return tmp_path


# ---------------------------------------------------------- validation


def test_rejects_unparseable_content(project):
    idx = _index(project)
    r = resolve_replace_symbol(idx, "services.helper", "def (: pass", project)
    assert isinstance(r, ReplaceSymbolResult)
    assert r.error_code == "parse_broken"


def test_rejects_multiple_top_level_defs(project):
    idx = _index(project)
    content = "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"
    r = resolve_replace_symbol(idx, "services.helper", content, project)
    assert isinstance(r, ReplaceSymbolResult)
    assert r.error_code == "invalid_argument"


def test_rejects_kind_mismatch(project):
    """Can't replace a function with a class."""
    idx = _index(project)
    content = "class helper:\n    pass\n"
    r = resolve_replace_symbol(idx, "services.helper", content, project)
    assert isinstance(r, ReplaceSymbolResult)
    assert r.error_code == "invalid_argument"


def test_rejects_name_collision_on_rename(project):
    """Renaming to an existing sibling should refuse."""
    idx = _index(project)
    content = "class OrderService:\n    def save(self, user):\n        return user\n"
    r = resolve_replace_symbol(idx, "services.UserService", content, project)
    assert isinstance(r, ReplaceSymbolResult)
    assert r.error_code == "name_collision"


def test_symbol_not_found(project):
    idx = _index(project)
    r = resolve_replace_symbol(idx, "services.missing", "def missing():\n    pass\n", project)
    assert isinstance(r, ReplaceSymbolResult)
    assert r.error_code == "symbol_not_found"


# ---------------------------------------------------------- same-name rewrite


def test_replace_same_name_is_single_file(project):
    idx = _index(project)
    content = "def helper():\n    return 99  # rewritten\n"
    req = resolve_replace_symbol(idx, "services.helper", content, project)
    assert isinstance(req, ReplaceSymbolRequest)
    assert req.name_changed is False
    assert len(req.edits) == 1

    result = apply_replace_symbol(req, project_root=project)
    assert result.status == "applied"
    assert result.name_changed is False

    text = (project / "services.py").read_text()
    assert "return 99  # rewritten" in text
    assert "return 42" not in text
    # Callers untouched.
    assert "helper()" in (project / "caller.py").read_text()


def test_replace_function_body_updates_signature(project):
    idx = _index(project)
    content = (
        "def helper(extra=None):\n"
        "    return 99 if extra else 42\n"
    )
    req = resolve_replace_symbol(idx, "services.helper", content, project)
    assert isinstance(req, ReplaceSymbolRequest)
    assert req.new_signature.startswith("def helper(")

    apply_replace_symbol(req, project_root=project)
    text = (project / "services.py").read_text()
    assert "def helper(extra=None)" in text


# ---------------------------------------------------------- rewrite + rename


def test_replace_with_rename_updates_callers(project):
    """New content declares a different name → refs in other files must update."""
    idx = _index(project)
    content = (
        "def fetch_meaning():\n"
        "    return 42\n"
    )
    req = resolve_replace_symbol(idx, "services.helper", content, project)
    assert isinstance(req, ReplaceSymbolRequest)
    assert req.name_changed is True
    assert req.new_leaf == "fetch_meaning"

    result = apply_replace_symbol(req, project_root=project)
    assert result.status == "applied"
    assert result.name_changed is True
    assert result.files_changed == 2

    services = (project / "services.py").read_text()
    caller = (project / "caller.py").read_text()

    assert "def fetch_meaning" in services
    assert "def helper" not in services
    assert "fetch_meaning()" in caller
    assert "from services import UserService, fetch_meaning" in caller


def test_replace_with_rename_word_boundary(tmp_path):
    (tmp_path / "m.py").write_text(
        "def save():\n    return 1\n\n\ndef use():\n    saved = 42  # must stay\n    return save()\n"
    )
    _git_init(tmp_path)

    idx = _index(tmp_path)
    content = "def persist():\n    return 2\n"
    req = resolve_replace_symbol(idx, "m.save", content, tmp_path)
    assert isinstance(req, ReplaceSymbolRequest)
    assert req.name_changed is True

    apply_replace_symbol(req, project_root=tmp_path)
    text = (tmp_path / "m.py").read_text()
    assert "def persist" in text
    assert "def save" not in text
    assert "saved = 42" in text  # substring preserved
    assert "return persist()" in text


# ---------------------------------------------------------- dry run


def test_dry_run_does_not_write(project):
    idx = _index(project)
    before = (project / "services.py").read_text()

    content = "def helper():\n    return 99\n"
    req = resolve_replace_symbol(idx, "services.helper", content, project)
    assert isinstance(req, ReplaceSymbolRequest)

    result = apply_replace_symbol(req, project_root=project, dry_run=True)
    assert result.status == "dry_run"
    assert (project / "services.py").read_text() == before


# ---------------------------------------------------------- decorator handling


def test_replace_decorated_class_with_decorator_in_content(tmp_path):
    """Bug 2: previously stacked the new decorator on top of the old one.

    Symbol byte range now starts at the first decorator, so passing content
    that includes the decorator splices it cleanly.
    """
    (tmp_path / "m.py").write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True, slots=True)\n"
        "class MarkerMatch:\n"
        "    x: int\n"
    )
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "i"],
        cwd=tmp_path, capture_output=True, check=True,
    )

    idx = _index(tmp_path)
    new = (
        "@dataclass(frozen=True, slots=True)\n"
        "class MarkerMatch:\n"
        "    x: int\n"
        "    y: int\n"
    )
    req = resolve_replace_symbol(idx, "m.MarkerMatch", new, tmp_path)
    assert isinstance(req, ReplaceSymbolRequest)
    apply_replace_symbol(req, project_root=tmp_path)

    out = (tmp_path / "m.py").read_text()
    assert out.count("@dataclass") == 1, f"decorator stacked:\n{out}"


# ---------------------------------------------------------- stale-index guard


def test_replace_refreshes_index_when_file_edited_out_of_band(tmp_path):
    """Bug 1: ReplaceSymbol must re-resolve byte ranges when file changed."""
    (tmp_path / "m.py").write_text(
        "X = 1\n"
        "def preclassify(n):\n"
        "    return n\n"
    )
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "i"],
        cwd=tmp_path, capture_output=True, check=True,
    )

    idx = _index(tmp_path)
    # Out-of-band edit shifts byte offsets.
    import time as _t
    _t.sleep(0.01)
    (tmp_path / "m.py").write_text(
        "X = 1\n"
        "Y = 2  # extra line shifts byte ranges\n"
        "def preclassify(n):\n"
        "    return n\n"
    )

    new = "def preclassify(n: int) -> int:\n    return n\n"
    req = resolve_replace_symbol(idx, "m.preclassify", new, tmp_path)
    assert isinstance(req, ReplaceSymbolRequest), req
    apply_replace_symbol(req, project_root=tmp_path)

    out = (tmp_path / "m.py").read_text()
    # Result must parse cleanly — pre-fix produced corrupt fragments.
    compile(out, "<test>", "exec")
    assert out.count("def preclassify") == 1
    assert "def preclassify(n: int) -> int:" in out


# ---------------------------------------------------------- atomicity


def test_replace_does_not_create_git_commits(project):
    """Replace is transactional via pre-image rollback — no git commits."""
    idx = _index(project)
    content = "def fetch_meaning():\n    return 42\n"
    req = resolve_replace_symbol(idx, "services.helper", content, project)
    assert isinstance(req, ReplaceSymbolRequest)
    before_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True
    ).stdout.strip()

    result = apply_replace_symbol(req, project_root=project)
    assert result.status == "applied"

    log = subprocess.run(
        ["git", "log", "--oneline", f"{before_sha}..HEAD"],
        cwd=project,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    assert log == []  # no commits created
    # Transaction is persisted instead.
    tx_dirs = list((project / ".symbol" / "transactions").iterdir())
    assert any("replace-symbol" in p.name for p in tx_dirs)
