"""The Rust binary answers the Python CLI's argv with the Python CLI's output.

Each scenario runs the same argv sequence through both CLIs on twin copies
of a fixture and compares stdout and exit codes. Timestamps (transaction
ids, build/save timings) are masked. Skipped unless the Rust binary is
built (`cargo build --release --manifest-path rust/Cargo.toml -p
symbol-core`) or SYMBOL_RUST_BIN points at one.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _rust_bin() -> Path | None:
    candidates = [os.environ.get("SYMBOL_RUST_BIN", "")] + [
        str(REPO / "rust/target" / profile / "symbol") for profile in ("release", "debug")
    ]
    return next((Path(c) for c in candidates if c and Path(c).is_file()), None)


RUST = _rust_bin()
PYTHON = Path(sys.executable).parent / "symbol"

pytestmark = pytest.mark.skipif(RUST is None, reason="Rust symbol binary not built")

APP = '''import os


class Service:
    """Doc — with unicode."""

    def save(self, x):
        return os.path.join(x)


def helper(a, b=1):
    s = Service()
    return s.save(a)
'''
CLI = "from app import helper\n\n\ndef main():\n    return helper(1)\n"
BIG = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(40))

READS = [
    ["search", "save"],
    ["search", "nope"],
    ["search", "-E", r"^app\.", "--limit", "2"],
    ["search", "save", "--format", "json"],
    ["--format", "json", "search", "save"],
    ["search", "save", "--regex", "--fixed"],
    ["code", "app.Service.save"],
    ["code", "helper", "--no-refs"],
    ["code", "nope"],
    ["code", "nope", "--format", "json"],
    ["code", "app.py:8-8", "--format", "json"],
    ["code", "app.py:1-2", "-f", "json"],
    ["outline", "app.py"],
    ["outline", "app.py", "--format", "json"],
    ["outline", "nope.py"],
    ["callers", "save"],
    ["callers", "save", "--format", "json"],
    ["loc", ".", "--format", "json"],
    ["refresh", "--format", "json"],
    ["undo", "--agent"],
]

WRITES = [
    ["patch", "app.py", "--range", "1-1", "--content", "import os  # x\n", "--format", "json"],
    ["patch", "app.py", "-r", "1-1", "--content", "import os  # y\n", "--force", "--format", "json"],
    ["patch", "app.py", "-r", "1-1", "--content", "import os\n", "--force", "--dry-run"],
    ["patch", "app.py", "-r", "900-901", "--content", "x", "--force", "--agent"],
    ["patch", "missing.py", "-r", "1-1", "--content", "x", "--force", "--format", "json"],
    ["delete-symbol", "app.helper", "--format", "json"],
    ["delete-symbol", "app.helper"],
    ["delete-symbol", "big.f3", "--dry-run", "-C", "1"],
    ["delete-symbol", "big.f4", "--format", "json"],
    ["insert-symbol", "big.f5", "--position", "after", "--content", "def g():\n    return 0\n", "--format", "json"],
    ["insert-symbol", "big.f7", "--position", "start", "--content", "y = 1\n", "--agent"],
    ["insert-symbol", "big.f8", "--position", "bogus", "--content", "y = 1\n", "--format", "json"],
    ["rename-symbol", "big.f9", "f9_renamed", "--format", "json"],
    ["undo", "--format", "json"],
    ["rename-symbol", "big.f9", "f9_renamed", "--dry-run"],
    ["rename-symbol", "big.f10", "bad name", "--format", "json"],
    ["replace-symbol", "big.f12", "--content", "def f12_new(y):\n    return y\n", "--format", "json"],
    ["undo"],
    ["replace-symbol", "big.f13", "--content", "def f13(x):\n    return 13\n", "--agent"],
    ["replace-symbol", "big.f14", "--content", "class Nope:\n    pass\n", "--format", "json"],
    ["refresh"],
]

_MASKS = [
    (re.compile(r"\d{13}-(rename|replace)-symbol"), r"<ts>-\1-symbol"),
    (re.compile(r"build \d+ ms  •  save \d+ ms"), "build <ms>  •  save <ms>"),
]


def _fixture(root: Path) -> Path:
    root.mkdir()
    (root / "app.py").write_text(APP)
    (root / "cli.py").write_text(CLI)
    (root / "big.py").write_text(BIG)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _run(binary: Path, argv: list[str], cwd: Path) -> tuple[int, str]:
    env = dict(os.environ, SYMBOL_SESSION_ID="parity", COLUMNS="80")
    env.pop("SYMBOL_AGENT", None)
    out = subprocess.run([str(binary), *argv], cwd=cwd, capture_output=True, text=True, env=env)
    text = out.stdout.replace(str(cwd), "<root>")
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return out.returncode, text


@pytest.mark.parametrize("steps", [READS, WRITES], ids=["reads", "writes"])
def test_rust_matches_python_output(tmp_path: Path, steps: list[list[str]]):
    py_root = _fixture(tmp_path / "py")
    rs_root = _fixture(tmp_path / "rs")
    for binary, root in ((PYTHON, py_root), (RUST, rs_root)):
        _run(binary, ["index"], root)
    mismatches = []
    for argv in steps:
        py = _run(PYTHON, argv, py_root)
        rs = _run(RUST, argv, rs_root)
        if py != rs:
            mismatches.append(f"$ symbol {' '.join(argv)}\n--- python {py}\n--- rust {rs}")
    shutil.rmtree(tmp_path, ignore_errors=True)
    assert not mismatches, "\n\n".join(mismatches)


def test_root_format_rejected_like_python(tmp_path: Path):
    root = _fixture(tmp_path / "p")
    for binary in (PYTHON, RUST):
        out = subprocess.run(
            [str(binary), "--format", "json", "index"], cwd=root, capture_output=True, text=True
        )
        assert out.returncode == 2
        # Click's error panel is colored and wrapped in CI; match the flattened text.
        text = re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", out.stderr))
        assert "'index' has no --format option" in " ".join(text.split())
