"""Vendored-directory pruning + MCP server lifecycle (issue #19).

The project walk must not descend into dependency trees (node_modules/,
vendor/, Godeps/, …) — that's the dominant CPU cost on large repos, and
GitHub Linguist already ships the patterns (``vendor.yml``) to recognize them.
The MCP server must also not outlive its launcher, or stale instances pile up
across sessions.
"""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from wyolet.symbol.shared.linguist import Blob, Linguist
from wyolet.symbol.shared.linguist.config.load import load_vendor_regex

# ---------------------------------------------------------------------------
# Combined vendor regex
# ---------------------------------------------------------------------------


class TestVendorRegex:
    def test_prunes_known_vendor_dirs(self):
        r = load_vendor_regex()
        for path in ("node_modules/", "vendor/", "sub/node_modules/",
                     "Godeps/_workspace/", "dist/"):
            assert r.search(path), f"expected {path} to be vendored"

    def test_keeps_real_source_dirs(self):
        r = load_vendor_regex()
        for path in ("src/", "src/wyolet/", "commands/", "app.py", "src/main.py"):
            assert not r.search(path), f"expected {path} to be kept"

    def test_matches_vendored_files(self):
        # Minified libs that don't live under a vendored directory.
        assert load_vendor_regex().search("static/jquery.min.js")


# ---------------------------------------------------------------------------
# Walk pruning
# ---------------------------------------------------------------------------


def _make_tree(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def real(): pass\n")
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.py").write_text("x = 1\n")
    (root / "vendor" / "dep").mkdir(parents=True)
    (root / "vendor" / "dep" / "lib.py").write_text("y = 2\n")


class TestWalkPruning:
    def test_vendored_dirs_excluded_by_default(self, tmp_path):
        _make_tree(tmp_path)
        langs = Linguist().classify_project(str(tmp_path))
        rels = {str(p.relative_to(tmp_path)) for p in langs}
        assert "src/app.py" in rels
        assert not any("node_modules" in r for r in rels)
        assert not any(r.startswith("vendor/") for r in rels)

    def test_skip_vendored_false_includes_them(self, tmp_path):
        _make_tree(tmp_path)
        langs = Linguist().classify_project(str(tmp_path), skip_vendored=False)
        rels = {str(p.relative_to(tmp_path)) for p in langs}
        assert any("node_modules" in r for r in rels)
        assert any(r.startswith("vendor/") for r in rels)

    def test_walk_does_not_descend_into_node_modules(self, tmp_path):
        # A vendored dir with many files should cost nothing past the prune —
        # assert via the result, not timing: none of them are enumerated.
        _make_tree(tmp_path)
        for i in range(50):
            (tmp_path / "node_modules" / "left-pad" / f"f{i}.py").write_text("z=0\n")
        paths = Linguist._get_all_file_paths(str(tmp_path))
        assert not any("node_modules" in p for p in paths)


# ---------------------------------------------------------------------------
# Blob memory safety — never slurp a giant file (the OOM regression)
# ---------------------------------------------------------------------------


class TestBlobMemoryCap:
    def test_large_file_is_not_fully_read(self, tmp_path):
        # A file well over the cap must hold at most MAX_READ_BYTES in memory.
        big = tmp_path / "relay-payloads.jsonl"
        line = b'{"x":1}\n'
        with big.open("wb") as f:
            for _ in range(Blob.MAX_READ_BYTES // len(line) + 50000):
                f.write(line)
        true_size = big.stat().st_size
        assert true_size > Blob.MAX_READ_BYTES

        blob = Blob(str(big))
        assert len(blob._content) <= Blob.MAX_READ_BYTES
        assert blob.truncated is True
        # .size must still reflect the real on-disk size, not the capped read.
        assert blob.size == true_size

    def test_small_file_read_in_full(self, tmp_path):
        small = tmp_path / "app.py"
        small.write_bytes(b"def f():\n    return 1\n")
        blob = Blob(str(small))
        assert blob.truncated is False
        assert blob.size == small.stat().st_size
        assert blob.read_lines()[0] == "def f():"

    def test_encoding_detection_bounded(self, tmp_path):
        # detect_encoding must only sniff the head, never the whole buffer.
        f = tmp_path / "data.txt"
        f.write_bytes(b"hello world\n" * 100000)
        blob = Blob(str(f))
        blob.encoding  # must not raise / must be bounded
        assert Blob.ENCODING_SNIFF_BYTES <= Blob.MAX_READ_BYTES


# ---------------------------------------------------------------------------
# MCP server lifecycle — orphan reaping
# ---------------------------------------------------------------------------


_ORPHAN_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    pid_file = sys.argv[1]
    # Fork a grandchild that installs the watchdog; the intermediate parent
    # exits immediately, orphaning the grandchild (reparented to init).
    pid = os.fork()
    if pid == 0:
        os.environ["SYMBOL_MCP_WATCHDOG_INTERVAL"] = "0.2"
        from wyolet.symbol.mcp.server import _install_orphan_watchdog
        _install_orphan_watchdog()
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
        time.sleep(30)  # watchdog should kill us well before this
    else:
        # Intermediate parent dies right away — grandchild is now orphaned.
        os._exit(0)
    """
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


_RUNAWAY_SCRIPT = textwrap.dedent(
    """
    import os, time
    os.environ["SYMBOL_MCP_MEM_LIMIT_MB"] = "300"
    os.environ["SYMBOL_MCP_WATCHDOG_INTERVAL"] = "0.1"
    from wyolet.symbol.mcp.server import _install_orphan_watchdog
    _install_orphan_watchdog()
    blocks = []
    for _ in range(40):
        blocks.append(bytearray(50 * 1024 * 1024))  # +50MB each, crosses 300MB
        time.sleep(0.05)
    print("NOT KILLED")  # watchdog should have exited 137 before here
    """
)


class TestMemoryWatchdog:
    def test_peak_rss_is_positive(self):
        from wyolet.symbol.mcp.server import _peak_rss_bytes
        assert _peak_rss_bytes() > 0

    def test_mem_limit_env_parsing(self, monkeypatch):
        from wyolet.symbol.mcp.server import _mem_limit_mb
        monkeypatch.setenv("SYMBOL_MCP_MEM_LIMIT_MB", "512")
        assert _mem_limit_mb() == 512
        monkeypatch.setenv("SYMBOL_MCP_MEM_LIMIT_MB", "garbage")
        assert _mem_limit_mb() == 2048  # falls back to default

    def test_hard_limit_call_is_safe(self, monkeypatch):
        # Best-effort: must never raise whether enabled or not.
        from wyolet.symbol.mcp.server import _apply_hard_mem_limit
        monkeypatch.delenv("SYMBOL_MCP_MEM_HARD", raising=False)
        _apply_hard_mem_limit()
        monkeypatch.setenv("SYMBOL_MCP_MEM_HARD", "1")
        monkeypatch.setenv("SYMBOL_MCP_MEM_LIMIT_MB", "4096")
        _apply_hard_mem_limit()

    def test_runaway_self_terminates(self):
        # A process that blows past the ceiling must be killed (exit 137).
        r = subprocess.run(
            [sys.executable, "-c", _RUNAWAY_SCRIPT],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 137, f"expected 137, got {r.returncode}: {r.stderr}"
        assert "ceiling" in r.stderr


class TestOrphanWatchdog:
    def test_orphaned_server_self_terminates(self, tmp_path):
        pid_file = tmp_path / "grandchild.pid"
        subprocess.run(
            [sys.executable, "-c", _ORPHAN_SCRIPT, str(pid_file)],
            check=True,
            timeout=10,
        )
        # Wait for the grandchild to record its pid.
        deadline = time.time() + 5
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "grandchild never started"
        pid = int(pid_file.read_text())

        # Orphaned (ppid==1) → watchdog (0.2s poll) should reap it quickly.
        deadline = time.time() + 5
        while _alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        assert not _alive(pid), "orphaned server did not self-terminate"
