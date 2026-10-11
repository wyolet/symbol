"""`symbol` console entry: hands Rust-implemented commands to the bundled binary.

Imports stay minimal (os.path, not pathlib) until the Python CLI is needed,
so the delegated path pays little beyond interpreter startup.
"""

import os
import sys

RUST_COMMANDS = frozenset(
    {
        "index",
        "refresh",
        "search",
        "code",
        "outline",
        "callers",
        "patch",
        "delete-symbol",
        "insert-symbol",
        "rename-symbol",
        "replace-symbol",
        "undo",
        "loc",
        "mcp",
        "hook",
    }
)

BIN_DIR = os.path.join(os.path.dirname(__file__), "bin")


def platform_name() -> str:
    """`<os>-<arch>` as used in bundled binary names (go-scan-<os>-<arch>)."""
    system = "windows" if sys.platform == "win32" else sys.platform
    if hasattr(os, "uname"):
        machine = os.uname().machine.lower()
    else:
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def bundled_rust_binary() -> str | None:
    suffix = ".exe" if sys.platform == "win32" else ""
    binary = os.path.join(BIN_DIR, f"symbol-{platform_name()}{suffix}")
    if os.path.isfile(binary) and os.access(binary, os.X_OK):
        return binary
    return None


def rust_binary_for(argv: list[str]) -> str | None:
    """The binary to run `argv` with, or None to stay in Python."""
    if os.environ.get("SYMBOL_IMPL") == "python":
        return None
    # Only a leading subcommand; global options before it are Python's.
    if len(argv) < 2 or argv[1] not in RUST_COMMANDS:
        return None
    return bundled_rust_binary()


def exec_rust_binary(binary: str, argv: list[str]) -> int:
    # pyscan runs on the interpreter this package is installed into.
    os.environ.setdefault("SYMBOL_PYTHON", sys.executable)
    if sys.platform != "win32":
        os.execv(binary, argv)
    import subprocess

    proc = subprocess.Popen(argv, executable=binary)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # The console delivers Ctrl+C to the child too; let it decide.
            continue


def run_python_cli() -> None:
    from .cli import app

    app()


def main() -> int | None:
    binary = rust_binary_for(sys.argv)
    if binary is not None:
        return exec_rust_binary(binary, sys.argv)
    run_python_cli()
    return None
