"""Rename through relative imports (`from .x import y`, `from .. import x`).

Relative import sources must resolve against the importing module before
they are compared with the rename target, otherwise import sites and their
call sites are skipped as mismatches and the rename leaves a broken import.
"""

import ast
import subprocess
from pathlib import Path

import pytest

from wyolet.symbol.adapters._python_ast_rename import _from_module
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


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _rename(root: Path, qpath: str, new_name: str):
    _git_init(root)
    idx, _ = get_or_build_index(root)
    req = resolve_rename_symbol(idx, qpath, new_name, root)
    assert isinstance(req, RenameSymbolRequest), f"resolve failed: {req}"
    return apply_rename_symbol(req, project_root=root, dry_run=False, _index=idx)


# ── resolution rule ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stmt", "module_prefix", "is_package", "expected"),
    [
        ("from x import y", "pkg.mod", False, "x"),
        ("from .helpers import y", "pkg.mod", False, "pkg.helpers"),
        ("from . import y", "pkg.mod", False, "pkg"),
        ("from ..helpers import y", "pkg.sub.mod", False, "pkg.helpers"),
        ("from .. import y", "pkg.sub.mod", False, "pkg"),
        ("from ...a.b import y", "pkg.sub.deep.mod", False, "pkg.a.b"),
        ("from .helpers import y", "pkg", True, "pkg.helpers"),
        ("from . import y", "pkg", True, "pkg"),
        ("from ..helpers import y", "pkg.sub", True, "pkg.helpers"),
        ("from .. import y", "pkg.sub", True, "pkg"),
        ("from ... import y", "pkg.mod", False, ""),
    ],
)
def test_from_module_resolves_relative_levels(stmt, module_prefix, is_package, expected):
    node = ast.parse(stmt).body[0]
    assert _from_module(node, module_prefix, is_package) == expected


# ── module-binding renames ─────────────────────────────────────────


def test_parent_relative_import_and_call(tmp_path):
    """`from ..helpers import f` two levels up, src layout (the flask shape)."""
    _write(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/helpers.py": "def get_root(name):\n    return name\n",
        "src/pkg/sub/__init__.py": "",
        "src/pkg/sub/scaffold.py": (
            "from ..helpers import get_root\n"
            "\n"
            "def build(name):\n"
            "    return get_root(name)\n"
        ),
    })
    result = _rename(tmp_path, "pkg.helpers.get_root", "get_root2")
    assert result.status == "applied", result
    assert not result.skipped_mismatch
    text = (tmp_path / "src/pkg/sub/scaffold.py").read_text()
    assert "from ..helpers import get_root2\n" in text
    assert "return get_root2(name)" in text


def test_sibling_relative_import(tmp_path):
    _write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/helpers.py": "def f():\n    return 1\n",
        "pkg/user.py": "from .helpers import f\n\nx = f()\n",
    })
    result = _rename(tmp_path, "pkg.helpers.f", "g")
    assert result.status == "applied", result
    assert (tmp_path / "pkg/user.py").read_text() == "from .helpers import g\n\nx = g()\n"


@pytest.mark.parametrize(
    ("user_rel", "import_line", "receiver"),
    [
        ("pkg/user.py", "from . import helpers", "helpers"),
        ("pkg/sub/user.py", "from .. import helpers", "helpers"),
        ("pkg/sub/user.py", "from .. import helpers as h", "h"),
    ],
)
def test_relative_module_import_attribute_access(tmp_path, user_rel, import_line, receiver):
    _write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/helpers.py": "def f():\n    return 1\n",
        user_rel: f"{import_line}\n\ndef use():\n    return {receiver}.f()\n",
    })
    result = _rename(tmp_path, "pkg.helpers.f", "g")
    assert result.status == "applied", result
    assert (tmp_path / user_rel).read_text() == (
        f"{import_line}\n\ndef use():\n    return {receiver}.g()\n"
    )


def test_relative_import_as_alias(tmp_path):
    """The imported name is rewritten; the local alias and its uses stay.
    The file holds no ref to the leaf, so it must still become a candidate."""
    _write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/helpers.py": "def f():\n    return 1\n",
        "pkg/sub/__init__.py": "",
        "pkg/sub/user.py": "from ..helpers import f as fn\n\nx = fn()\n",
    })
    result = _rename(tmp_path, "pkg.helpers.f", "g")
    assert result.status == "applied", result
    assert (tmp_path / "pkg/sub/user.py").read_text() == (
        "from ..helpers import g as fn\n\nx = fn()\n"
    )


def test_aliased_import_of_reexport(tmp_path):
    _write(tmp_path, {
        "pkg/__init__.py": "from .helpers import f\n",
        "pkg/helpers.py": "def f():\n    return 1\n",
        "app.py": "from pkg import f as fn\n\nx = fn()\n",
    })
    result = _rename(tmp_path, "pkg.helpers.f", "g")
    assert result.status == "applied", result
    assert (tmp_path / "app.py").read_text() == "from pkg import g as fn\n\nx = fn()\n"


def test_function_local_relative_import(tmp_path):
    _write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/helpers.py": "def f():\n    return 1\n",
        "pkg/user.py": "def use():\n    from .helpers import f\n    return f()\n",
    })
    result = _rename(tmp_path, "pkg.helpers.f", "g")
    assert result.status == "applied", result
    assert (tmp_path / "pkg/user.py").read_text() == (
        "def use():\n    from .helpers import g\n    return g()\n"
    )


def test_relative_import_inside_package_init(tmp_path):
    """`from .helpers import f` in `pkg/__init__.py` resolves to pkg.helpers,
    and `from ..helpers` in a subpackage `__init__` climbs one level."""
    _write(tmp_path, {
        "pkg/__init__.py": "from .helpers import f\n\nVALUE = f()\n",
        "pkg/helpers.py": "def f():\n    return 1\n",
        "pkg/sub/__init__.py": "from ..helpers import f\n",
    })
    result = _rename(tmp_path, "pkg.helpers.f", "g")
    assert result.status == "applied", result
    assert (tmp_path / "pkg/__init__.py").read_text() == "from .helpers import g\n\nVALUE = g()\n"
    assert (tmp_path / "pkg/sub/__init__.py").read_text() == "from ..helpers import g\n"


def test_reexport_through_package_init(tmp_path):
    """Consumer imports the re-export from the package; the package
    `__init__` imports it relatively from the defining module."""
    _write(tmp_path, {
        "pkg/__init__.py": "from .helpers import f\n",
        "pkg/helpers.py": "def f():\n    return 1\n",
        "app.py": "from pkg import f\n\nx = f()\n",
    })
    result = _rename(tmp_path, "pkg.helpers.f", "g")
    assert result.status == "applied", result
    assert (tmp_path / "pkg/__init__.py").read_text() == "from .helpers import g\n"
    assert (tmp_path / "app.py").read_text() == "from pkg import g\n\nx = g()\n"


def test_relative_import_of_same_leaf_elsewhere_untouched(tmp_path):
    """`from .other import f` names a different symbol: not rewritten."""
    _write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/helpers.py": "def f():\n    return 1\n",
        "pkg/other.py": "def f():\n    return 2\n",
        "pkg/user.py": "from .other import f\n\nx = f()\n",
    })
    _rename(tmp_path, "pkg.helpers.f", "g")
    assert (tmp_path / "pkg/user.py").read_text() == "from .other import f\n\nx = f()\n"


# ── member renames: receiver classes imported relatively ───────────


def test_member_receiver_class_imported_relatively(tmp_path):
    """With `save` owned by two classes, the annotation `User` must resolve
    through `from .models import User` to discriminate the receiver."""
    _write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/models.py": (
            "class User:\n"
            "    def save(self):\n"
            "        return 1\n"
            "\n"
            "class Post:\n"
            "    def save(self):\n"
            "        return 2\n"
        ),
        "pkg/sub/__init__.py": "",
        "pkg/sub/service.py": (
            "from ..models import Post, User\n"
            "\n"
            "def run(u: User, p: Post):\n"
            "    u.save()\n"
            "    p.save()\n"
        ),
    })
    result = _rename(tmp_path, "pkg.models.User.save", "persist")
    assert result.status == "applied", result
    text = (tmp_path / "pkg/sub/service.py").read_text()
    assert "    u.persist()\n" in text
    assert "    p.save()\n" in text
    assert not result.unresolved
