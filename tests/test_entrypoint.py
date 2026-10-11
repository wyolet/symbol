"""The `symbol` entry point delegates Rust commands to the bundled binary."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from wyolet.symbol import entrypoint

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="stub binary is a shell script")


class Execed(Exception):
    pass


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bin/ dir holding an executable stub named like the bundled binary."""
    suffix = ".exe" if sys.platform == "win32" else ""
    stub = tmp_path / f"symbol-{entrypoint.platform_name()}{suffix}"
    stub.write_text('#!/bin/sh\necho "rust $* python=$SYMBOL_PYTHON"\n')
    stub.chmod(0o755)
    monkeypatch.setattr(entrypoint, "BIN_DIR", str(tmp_path))
    monkeypatch.delenv("SYMBOL_IMPL", raising=False)
    monkeypatch.delenv("SYMBOL_PYTHON", raising=False)
    return stub


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


def run_main(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["symbol", *args])
    try:
        entrypoint.main()
    except Execed:
        pass


@posix_only
def test_rust_command_execs_bundled_binary(stub_bin, calls, monkeypatch):
    run_main(monkeypatch, "search", "UserService", "--kind", "class")
    assert calls == [("rust", stub_bin, ["symbol", "search", "UserService", "--kind", "class"], sys.executable)]


@posix_only
@pytest.mark.usefixtures("stub_bin")
def test_existing_symbol_python_is_kept(calls, monkeypatch):
    monkeypatch.setenv("SYMBOL_PYTHON", "/opt/python3.12")
    run_main(monkeypatch, "index")
    assert calls[0][3] == "/opt/python3.12"


@posix_only
@pytest.mark.usefixtures("stub_bin")
def test_impl_python_opts_out(calls, monkeypatch):
    monkeypatch.setenv("SYMBOL_IMPL", "python")
    run_main(monkeypatch, "search", "UserService")
    assert calls == [("python",)]


@posix_only
@pytest.mark.usefixtures("stub_bin")
@pytest.mark.parametrize("args", [["audit", "."], ["map", "."], ["init", "."], ["--version"], ["--format", "json", "loc", "."], []])
def test_other_invocations_stay_python(calls, monkeypatch, args):
    run_main(monkeypatch, *args)
    assert calls == [("python",)]


def test_missing_binary_falls_back(tmp_path, calls, monkeypatch):
    monkeypatch.setattr(entrypoint, "BIN_DIR", str(tmp_path))
    monkeypatch.delenv("SYMBOL_IMPL", raising=False)
    run_main(monkeypatch, "search", "UserService")
    assert calls == [("python",)]


@posix_only
def test_non_executable_binary_falls_back(stub_bin, calls, monkeypatch):
    stub_bin.chmod(0o644)
    run_main(monkeypatch, "search", "UserService")
    assert calls == [("python",)]


@posix_only
def test_real_exec_passes_argv_and_env(stub_bin):
    env = {k: v for k, v in os.environ.items() if k not in ("SYMBOL_IMPL", "SYMBOL_PYTHON")}
    code = (
        "import sys; from wyolet.symbol import entrypoint;"
        "entrypoint.BIN_DIR = sys.argv[1]; sys.argv = ['symbol', 'code', 'a.b']; entrypoint.main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(stub_bin.parent)], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == f"rust code a.b python={sys.executable}"
