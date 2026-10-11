"""The `symbol` entry point runs binary-implemented commands on the Rust binary."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wyolet.symbol import cli, entrypoint

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="stub binaries are shell scripts")


class Execed(Exception):
    pass


def make_stub(path: Path, label: str = "rust") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{label} $* python=$SYMBOL_PYTHON"\n')
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No binary anywhere: empty bundled dir, no checkout build, empty PATH."""
    for name in ("bin", "checkout", "path"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(entrypoint, "BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(entrypoint, "SOURCE_ROOT", str(tmp_path / "checkout"))
    monkeypatch.setenv("PATH", str(tmp_path / "path"))
    monkeypatch.delenv("SYMBOL_BIN", raising=False)
    monkeypatch.delenv("SYMBOL_PYTHON", raising=False)
    return tmp_path


@pytest.fixture
def bundled(isolated: Path) -> Path:
    return make_stub(isolated / "bin" / f"symbol-{entrypoint.platform_name()}")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """Records which implementation main() picks without leaving the process."""
    recorded: list = []

    def fake_execv(path, argv):
        recorded.append(("rust", Path(path), list(argv), os.environ.get("SYMBOL_PYTHON")))
        raise Execed

    monkeypatch.setattr(entrypoint.os, "execv", fake_execv)
    monkeypatch.setattr(entrypoint, "run_python_cli", lambda: recorded.append(("python",)))
    return recorded


def run_main(monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.setattr(sys, "argv", ["symbol", *args])
    try:
        return entrypoint.main()
    except Execed:
        return None


@posix_only
def test_delegated_command_execs_bundled_binary(bundled, calls, monkeypatch):
    run_main(monkeypatch, "search", "UserService", "--kind", "class")
    assert calls == [("rust", bundled, ["symbol", "search", "UserService", "--kind", "class"], sys.executable)]


@posix_only
@pytest.mark.usefixtures("bundled")
@pytest.mark.parametrize(
    "args",
    [
        ["--format", "json", "loc", "."],
        ["--format=json", "search", "x"],
        ["-v", "undo"],
        ["--root", ".", "--json", "index"],
        ["-p", ".", "refs", "a.B"],
    ],
)
def test_root_options_before_the_command_still_delegate(calls, monkeypatch, args):
    run_main(monkeypatch, *args)
    assert calls[0][0] == "rust"
    assert calls[0][2] == ["symbol", *args]


@posix_only
@pytest.mark.usefixtures("bundled")
def test_existing_symbol_python_is_kept(calls, monkeypatch):
    monkeypatch.setenv("SYMBOL_PYTHON", "/opt/python3.12")
    run_main(monkeypatch, "index")
    assert calls[0][3] == "/opt/python3.12"


@posix_only
@pytest.mark.usefixtures("bundled")
@pytest.mark.parametrize(
    "args", [["audit", "."], ["map", "."], ["init", "."], ["update-linguist"], ["--version"], ["--help"], ["."], []]
)
def test_python_commands_stay_python(calls, monkeypatch, args):
    run_main(monkeypatch, *args)
    assert calls == [("python",)]


@posix_only
@pytest.mark.usefixtures("bundled")
def test_symbol_bin_comes_first(isolated, calls, monkeypatch):
    override = make_stub(isolated / "custom" / "symbol")
    monkeypatch.setenv("SYMBOL_BIN", str(override))
    run_main(monkeypatch, "code", "a.b")
    assert calls[0][1] == override


@posix_only
@pytest.mark.parametrize("profiles", [["release", "debug"], ["debug"]])
def test_source_checkout_build_is_used(isolated, calls, monkeypatch, profiles):
    stubs = {p: make_stub(isolated / "checkout" / "rust" / "target" / p / "symbol") for p in profiles}
    run_main(monkeypatch, "outline", "app.py")
    assert calls[0][1] == stubs[profiles[0]]


@posix_only
def test_path_skips_python_entry_points(isolated, calls, monkeypatch):
    console_script = isolated / "venv-bin" / "symbol"
    console_script.parent.mkdir()
    console_script.write_text("#!/usr/bin/python3\nfrom wyolet.symbol.entrypoint import main\nmain()\n")
    console_script.chmod(0o755)
    binary = make_stub(isolated / "path" / "symbol")
    monkeypatch.setenv("PATH", os.pathsep.join([str(console_script.parent), str(binary.parent)]))
    run_main(monkeypatch, "callers", "save")
    assert calls[0][1] == binary


@posix_only
def test_path_skips_this_entry_point(isolated, monkeypatch):
    own = make_stub(isolated / "path" / "symbol")
    monkeypatch.setattr(sys, "argv", [str(own), "search", "x"])
    assert entrypoint.rust_binary() is None


@posix_only
def test_non_executable_binary_is_skipped(bundled, calls, monkeypatch):
    bundled.chmod(0o644)
    assert run_main(monkeypatch, "search", "UserService") == 1
    assert calls == []


def test_missing_binary_names_the_options(calls, monkeypatch, capsys):
    assert run_main(monkeypatch, "rename-symbol", "a.b", "c") == 1
    err = capsys.readouterr().err
    assert "`rename-symbol` runs on the Rust symbol binary, which was not found" in err
    for option in ("platform wheel", "github.com/wyolet/symbol/releases", "SYMBOL_BIN=", "cargo build"):
        assert option in err
    assert calls == []


def test_unusable_symbol_bin_is_reported(isolated, calls, monkeypatch, capsys):
    monkeypatch.setenv("SYMBOL_BIN", str(isolated / "missing"))
    assert run_main(monkeypatch, "search", "x") == 1
    assert f"SYMBOL_BIN={isolated / 'missing'} is not an executable symbol binary" in capsys.readouterr().err
    assert calls == []


def test_help_entries_delegate_argv_they_receive(monkeypatch):
    seen: list = []
    monkeypatch.setattr(cli, "run_delegated", lambda argv: seen.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["symbol", "search", "x", "--weird"])
    result = CliRunner().invoke(cli.app, ["search", "x", "--weird"])
    assert result.exit_code == 0, result.output
    assert seen == [["symbol", "search", "x", "--weird"]]


@posix_only
def test_real_exec_passes_argv_and_env(bundled):
    env = {k: v for k, v in os.environ.items() if k not in ("SYMBOL_BIN", "SYMBOL_PYTHON")}
    code = (
        "import sys; from wyolet.symbol import entrypoint;"
        "entrypoint.BIN_DIR = sys.argv[1]; sys.argv = ['symbol', 'code', 'a.b']; entrypoint.main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(bundled.parent)], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == f"rust code a.b python={sys.executable}"


def test_delegation_imports_no_cli_framework():
    code = (
        "import sys; from wyolet.symbol import entrypoint;"
        "entrypoint.exec_binary = lambda binary, argv: print(sorted("
        "m for m in sys.modules if m.split('.')[0] in ('typer', 'click', 'rich') or m == 'wyolet.symbol.cli')) or 0;"
        "entrypoint.rust_binary = lambda: 'symbol';"
        "sys.argv = ['symbol', 'search', 'x']; entrypoint.main()"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"
