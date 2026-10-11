"""`symbol` console entry: symbol-level commands run on the Rust `symbol`
binary; audit, analyze, dump, init, map and update-linguist run in Python.

Imports stay minimal (os.path, not pathlib) until the Python CLI is needed,
so the delegated path pays little beyond interpreter startup.
"""

import os
import sys

# Commands the binary implements, with the help line `symbol --help` shows.
DELEGATED_COMMANDS = {
    "index": "Build the symbol lookup table and write to .symbol/symbol_index.msgpack.zst.",
    "refresh": "Reindex changed files and clear transaction history.",
    "stats": "Index statistics.",
    "search": "Narrow candidates by name. Returns signatures, no bodies.",
    "code": "Retrieve exact body at a known address. Use after symbol search.",
    "outline": "Show a file's symbols as a parent-child tree.",
    "callers": "Tier-1 textual reference scan — name-match only, unresolved.",
    "refs": "Type-resolved references to a symbol (Go).",
    "implementers": "Implementers of an interface, or interfaces a type implements (Go).",
    "patch": "Byte-range edit primitive. Replace, delete (empty content), or insert (zero-width range).",
    "multi-patch": "Apply several non-overlapping edits to one file atomically.",
    "delete-symbol": "Remove a named symbol from its file. Refuses if callers exist (use --force).",
    "insert-symbol": "Insert code at a position anchored to a symbol (before/after/start/end).",
    "rename-symbol": "Rename a symbol and update references across the project.",
    "replace-symbol": "Replace a symbol's full definition. If content declares a new name, callers are updated too.",
    "undo": "Revert the most recent rename-symbol / replace-symbol transaction.",
    "loc": "Count lines of code by language — powered by GitHub Linguist.",
    "mcp": "Run the MCP server (stdio) exposing symbol as agent tools.",
    "hook": "Hook entry — reads Claude Code hook JSON from stdin.",
}

# Root options either CLI accepts ahead of the subcommand.
ROOT_FLAGS = frozenset({"-v", "--verbose", "--json"})
ROOT_VALUE_OPTIONS = frozenset({"--format", "--root", "--path", "-p"})

BIN_ENV = "SYMBOL_BIN"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(PACKAGE_DIR, "bin")
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(PACKAGE_DIR)))
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

MISSING_BINARY = """\
symbol: `{command}` runs on the Rust symbol binary, which was not found.{override}
Any of these provides it:
  - a platform wheel: pip install wyolet-symbol on macOS 13+ (arm64, x86_64),
    Linux (x86_64, arm64) or Windows (x86_64)
  - the standalone archive from https://github.com/wyolet/symbol/releases,
    with its `symbol` on PATH
  - {env}=/path/to/symbol
  - in a source checkout: cargo build --release --manifest-path rust/Cargo.toml -p symbol-core
"""


def platform_name() -> str:
    """`<os>-<arch>` as used in bundled binary names (symbol-<os>-<arch>)."""
    system = "windows" if sys.platform == "win32" else sys.platform
    if hasattr(os, "uname"):
        machine = os.uname().machine.lower()
    else:
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def subcommand(argv: list[str]) -> str | None:
    """The subcommand after any leading root options; None if there is none."""
    args = iter(argv[1:])
    for arg in args:
        name, has_value, _ = arg.partition("=")
        if arg in ROOT_FLAGS or (has_value and name in ROOT_VALUE_OPTIONS):
            continue
        if arg in ROOT_VALUE_OPTIONS:
            next(args, None)
            continue
        return None if arg.startswith("-") else arg
    return None


def is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def is_python_entry_point(path: str) -> bool:
    """This process's own script, or any install's console script for it;
    exec'ing one would loop back here."""
    try:
        if os.path.samefile(path, sys.argv[0]):
            return True
    except OSError:
        pass
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return head.startswith(b"#!") and b"wyolet.symbol.entrypoint" in head


def rust_binary() -> str | None:
    """SYMBOL_BIN, else the bundled binary, else a source checkout's cargo
    build, else a `symbol` on PATH that is not a Python entry point."""
    override = os.environ.get(BIN_ENV)
    if override:
        return override if is_executable(override) and not is_python_entry_point(override) else None
    candidates = [os.path.join(BIN_DIR, f"symbol-{platform_name()}{EXE_SUFFIX}")]
    candidates += [
        os.path.join(SOURCE_ROOT, "rust", "target", profile, f"symbol{EXE_SUFFIX}") for profile in ("release", "debug")
    ]
    for candidate in candidates:
        if is_executable(candidate):
            return candidate
    for directory in os.get_exec_path():
        candidate = os.path.join(directory, f"symbol{EXE_SUFFIX}")
        if is_executable(candidate) and not is_python_entry_point(candidate):
            return candidate
    return None


def exec_binary(binary: str, argv: list[str]) -> int:
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


def run_delegated(argv: list[str]) -> int:
    binary = rust_binary()
    if binary is not None:
        return exec_binary(binary, argv)
    override = os.environ.get(BIN_ENV)
    sys.stderr.write(
        MISSING_BINARY.format(
            command=subcommand(argv),
            env=BIN_ENV,
            override=f"\n{BIN_ENV}={override} is not an executable symbol binary." if override else "",
        )
    )
    return 1


def run_python_cli() -> None:
    from .cli import app

    app()


def main() -> int | None:
    if subcommand(sys.argv) in DELEGATED_COMMANDS:
        return run_delegated(sys.argv)
    run_python_cli()
    return None
