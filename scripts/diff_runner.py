"""Differential test RUNNER: Python symbol vs symbol-rs, driven by an ops
dataset. The dataset is authored elsewhere (generated per-repo by a model);
this runner only executes, diffs, times, and reports.

Usage:
  uv run scripts/diff_runner.py <repo> <ops.jsonl> [--report out.json]
  uv run scripts/diff_runner.py <repo> --index-check         # index diff only

Ops dataset: JSONL, one op per line.
  {"id": "r1", "op": "code",           "args": {"target": "app.models.UserService.save"}}
  {"id": "r2", "op": "callers",        "args": {"name": "save"}}
  {"id": "r3", "op": "outline",        "args": {"query": "app/models.py"}}
  {"id": "r4", "op": "search",         "args": {"patterns": ["save"], "kind": null,
                                                "file": null, "regex": false, "fixed": false,
                                                "ignore_case": false, "limit": 100}}
  {"id": "w1", "op": "rename-symbol",  "args": {"qualified_path": "...", "new_name": "..."},
               "tags": ["engine-kind"]}
  {"id": "w1p","op": "rename-parameter","args": {"func_qpath": "...", "param": "...",
                                                "new_name": "..."}}
  {"id": "w2", "op": "replace-symbol", "args": {"qualified_path": "...", "content": "..."}}
  {"id": "w3", "op": "delete-symbol",  "args": {"qualified_path": "...", "force": true}}
  {"id": "w4", "op": "insert-symbol",  "args": {"anchor_path": "...", "position": "after",
                                                "content": "...", "no_reindent": false}}
  {"id": "w5", "op": "patch",          "args": {"file": "rel/path.py", "range": "A-B",
                                                "content": "..."}}

Optional per-op keys:
  tags: ["engine-kind"] — divergence between v2-engine (Python) and tier-1
        (Rust) rename is classified known_divergence instead of MISMATCH.
  expect_error: true — both sides are expected to refuse; verdict compares
        the refusal (error_code) instead of tree state.

Verdicts: match | known_divergence | MISMATCH. Exit 1 if any MISMATCH.
Ground truth is the Python implementation — the runner never invents
expectations of its own.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUST_BIN = REPO / "rust/target/release/symbol-rs"

READ_OPS = {"code", "callers", "outline", "search"}
WRITE_OPS = {"rename-symbol", "rename-parameter", "replace-symbol", "delete-symbol", "insert-symbol", "patch"}


# ---------------------------------------------------------- process plumbing


def sh(cmd, cwd=None, env=None):
    t0 = time.perf_counter()
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env)
    return out, time.perf_counter() - t0


def run_rust(args, root):
    out, dt = sh([str(RUST_BIN), "--root", str(root), "--json", *args])
    try:
        return json.loads(out.stdout), dt, None
    except json.JSONDecodeError:
        return None, dt, (out.stdout + out.stderr).strip()[:400]


def tree_hash(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if rel.startswith((".symbol", ".git")) or not p.is_file():
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------- python side
# In-process (import once, index once) so 1k ops don't pay 1k interpreter
# startups. The Python implementation is the oracle.


class PySide:
    def __init__(self, root: Path):
        from wyolet.symbol.shared.symbol_index import get_or_build_index

        self.root = root
        self.index, _ = get_or_build_index(root)

    def read(self, op: str, args: dict):
        from wyolet.symbol.reads.callers import callers
        from wyolet.symbol.reads.code import code, CodeAmbiguous, CodeNotFound
        from wyolet.symbol.reads.outline import outline
        from wyolet.symbol.reads.search import search

        if op == "code":
            try:
                return code(self.index, args["target"])
            except CodeAmbiguous as e:
                return {"ambiguous": True, "candidates": e.candidates}
            except CodeNotFound as e:
                return {"error": "not_found", "message": str(e)}
        if op == "callers":
            return callers(self.index, args["name"])
        if op == "outline":
            return outline(self.index, args["query"])
        if op == "search":
            return search(
                self.index,
                args.get("patterns", []),
                kind=args.get("kind"),
                file=args.get("file"),
                regex=bool(args.get("regex")),
                fixed=bool(args.get("fixed")),
                ignore_case=bool(args.get("ignore_case")),
                limit=int(args.get("limit", 100)),
            )
        raise ValueError(op)


def py_write(op: str, args: dict, cwd: Path):
    """Writes go through the Python CLI in a fresh copy (its index state is
    per-copy, so in-process reuse buys nothing here)."""
    cmd = ["uv", "run", "--project", str(REPO), "symbol"]
    if op == "rename-symbol":
        cmd += ["rename-symbol", args["qualified_path"], args["new_name"]]
    elif op == "rename-parameter":
        cmd += ["rename-parameter", args["func_qpath"], args["param"], args["new_name"]]
    elif op == "replace-symbol":
        # Python CLI takes content as an OPTION (same class as the patch fix)
        cmd += ["replace-symbol", args["qualified_path"], "--content", args["content"]]
    elif op == "delete-symbol":
        cmd += ["delete-symbol", args["qualified_path"]] + (["--force"] if args.get("force") else [])
    elif op == "insert-symbol":
        cmd += ["insert-symbol", args["anchor_path"],
                "--position", args["position"], "--content", args.get("content", "")]
        cmd += ["--no-reindent"] if args.get("no_reindent") else []
    elif op == "patch":
        # Python CLI takes range/content as OPTIONS (verified: symbol patch --help)
        cmd += ["patch", args["file"], "--range", args["range"],
                "--content", args.get("content", ""), "--force"]
    cmd += ["--format", "json"]
    return sh(cmd, cwd=cwd)


def rust_write(op: str, args: dict, root: Path):
    a = ["--root", str(root), "--json"]
    if op == "rename-symbol":
        a += ["rename-symbol", args["qualified_path"], args["new_name"]]
    elif op == "rename-parameter":
        a += ["rename-parameter", args["func_qpath"], args["param"], args["new_name"]]
    elif op == "replace-symbol":
        a += ["replace-symbol", args["qualified_path"], args["content"]]
    elif op == "delete-symbol":
        a += ["delete-symbol", args["qualified_path"]] + (["--force"] if args.get("force") else [])
    elif op == "insert-symbol":
        a += ["insert-symbol", args["anchor_path"], args["position"], args.get("content", "")]
        a += ["--no-reindent"] if args.get("no_reindent") else []
    elif op == "patch":
        a += ["patch", str(root / args["file"]), args["range"], args.get("content", ""), "--force"]
    return sh([str(RUST_BIN), *a])


# ---------------------------------------------------------- normalization
# Canonical views per op so cosmetic differences (key order, extra keys)
# don't count, while every load-bearing field does.


def canon(op: str, payload):
    if payload is None:
        return None
    if op == "code":
        if isinstance(payload, dict) and payload.get("ambiguous"):
            return {"ambiguous": True, "n": len(payload.get("candidates", []))}
        if isinstance(payload, dict) and payload.get("error"):
            return {"error": True}
        keys = ["path", "file", "start_line", "end_line", "kind", "signature", "body"]
        return {k: payload.get(k) for k in keys}
    if op == "callers":
        return sorted(
            (c["source_file"], c["ref_line"], c["ref_kind"], c["source_path"])
            for c in payload
        )
    if op == "outline":
        def strip(nodes):
            return [
                {
                    "path": n["path"], "kind": n["kind"],
                    "start_line": n["start_line"], "end_line": n["end_line"],
                    "signature": n["signature"], "children": strip(n["children"]),
                }
                for n in nodes
            ]
        return strip(payload)
    if op == "search":
        return sorted(
            (h["path"], h["file"], h["start_line"], h["end_line"], h["kind"], h["signature"])
            for h in payload
        )
    return payload


# ---------------------------------------------------------- runner


class Runner:
    def __init__(self, target: Path):
        self.target = target
        self.verdicts = Counter()
        self.failures = []
        self.timings = defaultdict(lambda: {"py": [], "rs": []})
        self._copy_seq = 0

    def verdict(self, kind, op_id, op, detail=""):
        self.verdicts[kind] += 1
        if kind == "MISMATCH":
            self.failures.append({"id": op_id, "op": op, "detail": detail[:500]})

    # ---- phase: index structural diff (always available, no dataset needed)

    def index_check(self):
        from wyolet.symbol.shared.symbol_index import SymbolIndex

        py_copy = self.fresh_copy("idxpy")
        rs_copy = self.fresh_copy("idxrs")
        _, dt_py = sh(["uv", "run", "--project", str(REPO), "symbol", "index", str(py_copy)])
        self.timings["index"]["py"].append(dt_py)
        _, dt_rs = sh([str(RUST_BIN), "--root", str(rs_copy), "index"])
        self.timings["index"]["rs"].append(dt_rs)

        py_idx = SymbolIndex.load(py_copy)
        rs_idx = SymbolIndex.load(rs_copy)
        if py_idx is None or rs_idx is None:
            self.verdict("MISMATCH", "index", "index-diff",
                         f"load failed py={py_idx is None} rs={rs_idx is None}")
            return

        def snapshot(idx):
            rows = {}
            for qpath, ids in idx.by_path.items():
                rows[qpath] = sorted(
                    (idx.file_of(r), idx.kind_of(r), idx.range_of(r),
                     idx.byte_range_of(r), tuple(sorted(idx.refs_for(r))),
                     tuple(idx.bases_for(r)))
                    for r in ids
                )
            imports = sorted(
                (idx.files[imp[0]], idx.strings[imp[1]], idx.strings[imp[2]], imp[3])
                for imp in idx.imports if imp[1] != 0
            )
            return rows, imports

        py_rows, py_imports = snapshot(py_idx)
        rs_rows, rs_imports = snapshot(rs_idx)
        for qpath in sorted(set(py_rows) | set(rs_rows)):
            a, b = py_rows.get(qpath), rs_rows.get(qpath)
            if a == b:
                self.verdict("match", f"index:{qpath}", "index-diff")
            else:
                self.verdict("MISMATCH", f"index:{qpath}", "index-diff", f"py={a} rs={b}")
        if py_imports == rs_imports:
            self.verdict("match", "index:imports", "index-diff")
        else:
            only_py = sorted(set(py_imports) - set(rs_imports))[:5]
            only_rs = sorted(set(rs_imports) - set(py_imports))[:5]
            self.verdict("MISMATCH", "index:imports", "index-diff",
                         f"only_py={only_py} only_rs={only_rs}")
        shutil.rmtree(rs_copy, ignore_errors=True)
        self.read_copy = py_copy  # reads run against this shared copy

    # ---- phase: dataset ops

    def run_ops(self, ops: list[dict]):
        read_ops = [o for o in ops if o["op"] in READ_OPS]
        write_ops = [o for o in ops if o["op"] in WRITE_OPS]
        unknown = [o for o in ops if o["op"] not in READ_OPS | WRITE_OPS]
        for o in unknown:
            self.verdict("MISMATCH", o.get("id", "?"), o["op"], "unknown op")

        if read_ops:
            if not hasattr(self, "read_copy"):
                self.read_copy = self.fresh_copy("reads")
                sh(["uv", "run", "--project", str(REPO), "symbol", "index", str(self.read_copy)])
            py = PySide(self.read_copy)
            for o in read_ops:
                self.run_read(py, o)

        for o in write_ops:
            self.run_write(o)

    def run_read(self, py: "PySide", o: dict):
        op, args, op_id = o["op"], o.get("args", {}), o.get("id", "?")
        t0 = time.perf_counter()
        try:
            py_out = py.read(op, args)
        except Exception as e:  # oracle crashed — that's a finding too
            py_out = {"error": "exception", "message": str(e)}
        self.timings[op]["py"].append(time.perf_counter() - t0)

        rs_args = {
            "code": lambda: ["code", args["target"]],
            "callers": lambda: ["callers", args["name"]],
            "outline": lambda: ["outline", args["query"]],
            "search": lambda: ["search", *args.get("patterns", [])]
            + (["--kind", args["kind"]] if args.get("kind") else [])
            + (["--file", args["file"]] if args.get("file") else [])
            + (["--regex"] if args.get("regex") else [])
            + (["--fixed"] if args.get("fixed") else [])
            + (["--ignore-case"] if args.get("ignore_case") else [])
            + ["--limit", str(args.get("limit", 100))],
        }[op]()
        rs_out, dt, err = run_rust(rs_args, self.read_copy)
        self.timings[op]["rs"].append(dt)

        if err and isinstance(py_out, dict) and py_out.get("error"):
            self.verdict("match", op_id, op)  # both refused
            return
        if err:
            self.verdict("MISMATCH", op_id, op, f"rust failed, python didn't: {err}")
            return
        a, b = canon(op, py_out), canon(op, rs_out)
        if a == b:
            self.verdict("match", op_id, op)
        else:
            self.verdict("MISMATCH", op_id, op, json.dumps({"py": a, "rs": b}, default=str)[:500])

    def run_write(self, o: dict):
        op, args, op_id = o["op"], o.get("args", {}), o.get("id", "?")
        tags = set(o.get("tags", []))
        py_copy = self.fresh_copy("wpy")
        rs_copy = self.fresh_copy("wrs")
        pristine = tree_hash(py_copy)

        py_out, dt_py = py_write(op, args, py_copy)
        self.timings[op]["py"].append(dt_py)
        rs_out, dt_rs = rust_write(op, args, rs_copy)
        self.timings[op]["rs"].append(dt_rs)

        after_py, after_rs = tree_hash(py_copy), tree_hash(rs_copy)
        py_changed = after_py != pristine
        rs_changed = after_rs != pristine

        if o.get("expect_error"):
            both_refused = not py_changed and not rs_changed
            self.verdict("match" if both_refused else "MISMATCH", op_id, op,
                         "" if both_refused else
                         f"expected refusal; py_changed={py_changed} rs_changed={rs_changed}")
        elif after_py == after_rs:
            self.verdict("match", op_id, op)
        elif "engine-kind" in tags and op == "rename-symbol":
            diff_files = sorted(k for k in set(after_py) | set(after_rs)
                                if after_py.get(k) != after_rs.get(k))
            self.verdict("known_divergence", op_id, op, f"tier1-vs-engine {diff_files[:4]}")
        else:
            diff_files = sorted(k for k in set(after_py) | set(after_rs)
                                if after_py.get(k) != after_rs.get(k))
            self.verdict("MISMATCH", op_id, op, f"trees differ: {diff_files[:6]}")

        # undo both sides; each must restore pristine — TRANSACTION-BACKED
        # ops only. insert-symbol/delete-symbol/patch are direct byte-writes
        # with no transaction (by design, identically on both sides), so
        # `symbol undo` is a no-op for them and asserting pristine would
        # manufacture false MISMATCHes.
        tx_backed = op in {"rename-symbol", "replace-symbol", "rename-parameter"}
        if tx_backed and py_changed:
            sh(["uv", "run", "--project", str(REPO), "symbol", "undo"], cwd=py_copy)
            self.verdict("match" if tree_hash(py_copy) == pristine else "MISMATCH",
                         f"{op_id}/undo-py", op, "python undo not pristine")
        if tx_backed and rs_changed:
            run_rust(["undo"], rs_copy)
            self.verdict("match" if tree_hash(rs_copy) == pristine else "MISMATCH",
                         f"{op_id}/undo-rs", op, "rust undo not pristine")

        shutil.rmtree(py_copy, ignore_errors=True)
        shutil.rmtree(rs_copy, ignore_errors=True)

    # ---- plumbing

    def fresh_copy(self, tag) -> Path:
        self._copy_seq += 1
        dst = Path(f"/tmp/symdiff-{self.target.name}-{tag}-{self._copy_seq}")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(self.target, dst, ignore=shutil.ignore_patterns(".symbol"))
        return dst.resolve()  # /tmp is a symlink on macOS; both CLIs canonicalize

    def report(self):
        def med(xs):
            return round(sorted(xs)[len(xs) // 2], 4) if xs else None

        return {
            "target": str(self.target),
            "verdicts": dict(self.verdicts),
            "failures": self.failures[:50],
            "timing_median_s": {
                op: {"py": med(v["py"]), "rs": med(v["rs"]), "n": max(len(v["py"]), len(v["rs"]))}
                for op, v in self.timings.items()
            },
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("ops", nargs="?", help="ops dataset (JSONL)")
    ap.add_argument("--index-check", action="store_true")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    runner = Runner(Path(args.target).resolve())
    if args.index_check or not args.ops:
        runner.index_check()
    if args.ops:
        ops = [json.loads(l) for l in Path(args.ops).read_text().splitlines() if l.strip()]
        runner.run_ops(ops)

    report = runner.report()
    print(json.dumps({"verdicts": report["verdicts"],
                      "timing_median_s": report["timing_median_s"]}, indent=2))
    for f in report["failures"][:15]:
        print(f"MISMATCH {f['id']} ({f['op']}): {f['detail'][:220]}")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, default=str))
        print(f"report → {args.report}")
    sys.exit(1 if runner.verdicts.get("MISMATCH") else 0)


if __name__ == "__main__":
    main()
