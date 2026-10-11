"""The CLI surface of the commands the Rust binary runs, as the Python CLI
had it, frozen in tests/fixtures/python_cli_surface.json.

rust/symbol/src/cli_surface_tests.rs checks the binary accepts every command,
option, short flag and default recorded there. The command entries change
only by hand, to change that contract on purpose; the root options are still
the Python CLI's and are compared against it live.
"""

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from wyolet.symbol.cli import app
from wyolet.symbol.entrypoint import DELEGATED_COMMANDS

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "python_cli_surface.json").read_text())
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


def test_root_options_match_snapshot():
    group = typer.main.get_command(app)
    assert SNAPSHOT["root"] == [_param(p) for p in group.params if p.name not in COMPLETION], (
        "root options changed: mirror them in the Rust CLI, then update the snapshot"
    )


def test_frozen_commands_are_delegated():
    assert set(SNAPSHOT["commands"]) <= set(DELEGATED_COMMANDS)


def test_help_lists_delegated_commands():
    group = typer.main.get_command(app)
    assert {name for name in DELEGATED_COMMANDS if group.commands[name].hidden} == {"hook"}
    result = CliRunner().invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    for name in DELEGATED_COMMANDS.keys() - {"hook"}:
        assert f" {name} " in result.output, name
