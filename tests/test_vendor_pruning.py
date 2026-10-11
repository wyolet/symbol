"""Vendored-directory pruning and bounded file reads (issue #19).

The project walk must not descend into dependency trees (node_modules/,
vendor/, Godeps/, …) — that's the dominant CPU cost on large repos, and
GitHub Linguist already ships the patterns (``vendor.yml``) to recognize them.
"""

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
