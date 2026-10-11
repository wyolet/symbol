"""The Python CLI's argument surface for the commands the Rust binary also
implements, pinned in tests/fixtures/python_cli_surface.json.

The Rust test suite checks the binary accepts every command, option, short
flag and default recorded there, so a change here must land in both CLIs.
Refresh the snapshot with `SYMBOL_UPDATE_CLI_SURFACE=1 uv run pytest
tests/test_cli_surface.py`.
"""

import json
import os
from pathlib import Path

import typer

from wyolet.symbol.cli import app

SNAPSHOT = Path(__file__).parent / "fixtures" / "python_cli_surface.json"

DELEGATED = [
    "index", "refresh", "search", "code", "outline", "callers", "patch",
    "delete-symbol", "insert-symbol", "rename-symbol", "replace-symbol",
    "undo", "loc", "hook",
]
# Typer's shell-completion installers: root-only, never reach a subcommand.
COMPLETION = {"install_completion", "show_completion"}


def _param(p) -> dict:
    default = p.default
    if not (default is None or isinstance(default, (str, int, float, bool))):
        default = str(default)
    return {
        "name": p.name,
        "kind": p.param_type_name,
        "opts": list(p.opts) + list(p.secondary_opts),
        "required": p.required,
        "multiple": bool(p.multiple or p.nargs == -1),
        "flag": bool(getattr(p, "is_flag", False)),
        "type": p.type.name,
        "default": default,
    }


def surface() -> dict:
    group = typer.main.get_command(app)
    return {
        "root": [_param(p) for p in group.params if p.name not in COMPLETION],
        "commands": {name: [_param(p) for p in group.commands[name].params] for name in DELEGATED},
    }


def test_snapshot_matches_python_cli():
    current = surface()
    if os.environ.get("SYMBOL_UPDATE_CLI_SURFACE"):
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n")
    assert json.loads(SNAPSHOT.read_text()) == current, (
        "Python CLI surface changed: mirror it in the Rust CLI, then refresh "
        "the snapshot (see module docstring)"
    )
