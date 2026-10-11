"""`symbol hook`: PreToolUse nudges / --enforce blocks, PostToolUse refresh.

Drives the Python CLI, and the Rust binary when it is built (see
test_rust_cli_parity.py) — both must print the same bytes.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_rust_cli_parity import RUST

PYTHON = Path(sys.executable).parent / "symbol"
IMPLEMENTATIONS = [pytest.param(PYTHON, id="python")]
if RUST is not None:
    IMPLEMENTATIONS.append(pytest.param(RUST, id="rust"))

BIG = "".join(f"def step_{i}(x):\n    return x + {i}\n\n\n" for i in range(30))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = (tmp_path / "proj").resolve()
    (root / "app").mkdir(parents=True)
    (root / "app" / "big.py").write_text(BIG)
    (root / "app" / "small.py").write_text("def tiny():\n    return 1\n")
    (root / "README.md").write_text("# notes\n" * 120)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([str(PYTHON), "index", str(root)], check=True, capture_output=True)
    return root


def _hook(binary: Path, payload, *args: str, cwd: Path | None = None):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    env = {k: v for k, v in os.environ.items() if k != "SYMBOL_AGENT"}
    return subprocess.run(
        [str(binary), "hook", *args], input=raw, capture_output=True, text=True,
        cwd=cwd, env=env, timeout=60,
    )


def _pre(root: Path, tool: str, **tool_input) -> dict:
    return {"hook_event_name": "PreToolUse", "cwd": str(root), "tool_name": tool, "tool_input": tool_input}


def _context(out) -> str:
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("binary", IMPLEMENTATIONS)
def test_read_of_indexed_file_is_nudged(binary, project):
    out = _hook(binary, _pre(project, "Read", file_path=str(project / "app/big.py")))
    payload = json.loads(out.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PreToolUse"
    assert payload["permissionDecision"] == "allow"
    assert payload["additionalContext"].startswith('symbol: "app/big.py" is indexed (python).')


@pytest.mark.parametrize("binary", IMPLEMENTATIONS)
@pytest.mark.parametrize("tool,target", [
    ("Read", "app/small.py"),     # under the small-file threshold
    ("Read", "README.md"),        # no adapter handles it
    ("Edit", "app/missing.py"),   # not a file
    ("Write", "app/new.py"),      # creating, not overwriting
    ("Bash", "app/big.py"),       # not a file tool
])
def test_silent_cases(binary, project, tool, target):
    out = _hook(binary, _pre(project, tool, file_path=str(project / target)))
    assert (out.returncode, out.stdout, out.stderr) == (0, "", "")


@pytest.mark.parametrize("binary", IMPLEMENTATIONS)
def test_grep_for_a_declaration_is_nudged(binary, project):
    message = _context(_hook(binary, _pre(project, "Grep", pattern="step_1")))
    assert '"step_1" is the name of 1 indexed declaration(s) (e.g. app.big.step_1)' in message
    regex = _context(_hook(binary, _pre(project, "Grep", pattern="step_[0-9]+")))
    assert "is the name of 5+ indexed declaration(s)" in regex
    miss = _hook(binary, _pre(project, "Grep", pattern="nothing_like_this"))
    assert (miss.returncode, miss.stdout) == (0, "")


@pytest.mark.parametrize("binary", IMPLEMENTATIONS)
def test_enforce_blocks_with_reason_on_stderr(binary, project):
    out = _hook(binary, _pre(project, "Edit", file_path=str(project / "app/big.py")), "--enforce")
    assert out.returncode == 2
    assert out.stdout == ""
    assert out.stderr.startswith('symbol: "app/big.py" is indexed (python). Edit requires')


@pytest.mark.parametrize("binary", IMPLEMENTATIONS)
def test_no_index_or_no_project_is_a_no_op(binary, project, tmp_path):
    (project / ".symbol" / "symbol_index.msgpack.zst").unlink()
    out = _hook(binary, _pre(project, "Read", file_path=str(project / "app/big.py")))
    assert (out.returncode, out.stdout) == (0, "")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    out = _hook(binary, _pre(outside, "Grep", pattern="step_1"))
    assert (out.returncode, out.stdout) == (0, "")
    out = _hook(binary, "not json")
    assert (out.returncode, out.stdout) == (0, "")


@pytest.mark.parametrize("binary", IMPLEMENTATIONS)
def test_post_edit_refreshes_the_file_in_the_background(binary, project):
    big = project / "app/big.py"
    big.write_text(BIG + "def added_by_edit():\n    return 0\n")
    event = {
        "hook_event_name": "PostToolUse", "cwd": str(project), "tool_name": "Edit",
        "tool_input": {"file_path": str(big)}, "tool_response": {},
    }
    out = _hook(binary, event)
    assert (out.returncode, out.stdout) == (0, "")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found = _hook(binary, _pre(project, "Grep", pattern="added_by_edit"))
        if found.stdout:
            break
        time.sleep(0.2)
    assert "app.big.added_by_edit" in found.stdout


@pytest.mark.skipif(RUST is None, reason="Rust symbol binary not built")
def test_rust_prints_the_same_bytes(project):
    events = [
        (_pre(project, "Read", file_path=str(project / "app/big.py")), ()),
        (_pre(project, "Write", file_path=str(project / "app/big.py")), ()),
        (_pre(project, "Edit", file_path=str(project / "app/big.py")), ()),
        (_pre(project, "Grep", pattern="step_2"), ()),
        (_pre(project, "Grep", pattern="step_[0-9]+"), ("--enforce",)),
    ]
    for event, args in events:
        py, rs = _hook(PYTHON, event, *args), _hook(RUST, event, *args)
        assert (py.returncode, py.stdout, py.stderr) == (rs.returncode, rs.stdout, rs.stderr), event
