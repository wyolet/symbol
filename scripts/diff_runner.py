"""Differential test RUNNER: the reference Python symbol vs the Rust symbol,
driven by an ops dataset. The dataset is authored elsewhere (generated
per-repo by a model); this runner only executes, diffs, times, and reports.

The reference is a checkout holding the Python implementation (the last such
commit is 238accc; setup in scripts/datasets/README.md), passed as
--reference or SYMBOL_REFERENCE. Its side runs through `uv run --project
<reference>`; scripts/diff_reference.py does the in-process index and read work.

Usage:
  uv run scripts/diff_runner.py <repo> <ops.jsonl> --reference <dir> [--report out.json]
  uv run scripts/diff_runner.py <repo> --reference <dir> --index-check   # index diff only

Ops dataset: JSONL, one op per line.
  {"id": "r1", "op": "code",           "args": {"target": "app.models.UserService.save"}}
  {"id": "r2", "op": "callers",        "args": {"name": "save"}}
  {"id": "r3", "op": "outline",        "args": {"query": "app/models.py"}}
  {"id": "r4", "op": "search",         "args": {"patterns": ["save"], "kind": null,
                                                "file": null, "regex": false, "fixed": false,
                                                "ignore_case": false, "limit": 100}}
  {"id": "w1", "op": "rename-symbol",  "args": {"qualified_path": "...", "new_name": "..."},
               "tags": ["engine-kind"]}
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

Verdicts: match | both_refused | known_divergence | MISMATCH. Exit 1 if any
MISMATCH. both_refused marks a non-expect_error write neither side applied.
Ground truth is the reference Python implementation — the runner never
invents expectations of its own.

MCP mode (--mcp) replays the same dataset as tool calls against both MCP
servers (`symbol mcp`), each on its own copy of the target, and compares
the full tool results (text payloads byte-for-byte, after replacing each
copy's root with <root>), plus tools/list and initialize. See McpRunner.
Tools only the Rust server lists get a rust_only verdict, not MISMATCH.
"""

import argparse
import difflib
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUST_BIN = REPO / "rust/target/release/symbol"
REFERENCE_HELPER = Path(__file__).resolve().parent / "diff_reference.py"
REFERENCE: Path  # set by main()

READ_OPS = {"code", "callers", "outline", "search"}
WRITE_OPS = {"rename-symbol", "replace-symbol", "delete-symbol", "insert-symbol", "patch"}


# ---------------------------------------------------------- process plumbing


def sh(cmd, cwd=None, env=None):
    t0 = time.perf_counter()
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env)
    return out, time.perf_counter() - t0


def ref_symbol(*args) -> list[str]:
    return ["uv", "run", "--project", str(REFERENCE), "symbol", *args]


def ref_helper(*args, stdin: str | None = None):
    out = subprocess.run(
        ["uv", "run", "--project", str(REFERENCE), "python", str(REFERENCE_HELPER), *map(str, args)],
        input=stdin, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"diff_reference.py {args[0]} failed: {out.stderr.strip()[-2000:]}")
    return json.loads(out.stdout)


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


def py_write(op: str, args: dict, cwd: Path):
    """Writes go through the reference CLI in a fresh copy."""
    cmd = ref_symbol()
    if op == "rename-symbol":
        cmd += ["rename-symbol", args["qualified_path"], args["new_name"]]
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
        py_copy = self.fresh_copy("idxpy")
        rs_copy = self.fresh_copy("idxrs")
        _, dt_py = sh(ref_symbol("index", str(py_copy)))
        self.timings["index"]["py"].append(dt_py)
        _, dt_rs = sh([str(RUST_BIN), "--root", str(rs_copy), "index"])
        self.timings["index"]["rs"].append(dt_rs)

        # The reference loads both indexes; the on-disk format is shared.
        snapshots = ref_helper("snapshot", py_copy, rs_copy)
        py_snap, rs_snap = snapshots[str(py_copy)], snapshots[str(rs_copy)]
        if py_snap is None or rs_snap is None:
            self.verdict("MISMATCH", "index", "index-diff",
                         f"load failed py={py_snap is None} rs={rs_snap is None}")
            return

        py_rows, py_imports = py_snap["rows"], [tuple(i) for i in py_snap["imports"]]
        rs_rows, rs_imports = rs_snap["rows"], [tuple(i) for i in rs_snap["imports"]]
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
                sh(ref_symbol("index", str(self.read_copy)))
            # One reference process for every read: index loaded once.
            py_results = ref_helper("reads", self.read_copy,
                                    stdin="\n".join(json.dumps(o) for o in read_ops))
            for o, py_result in zip(read_ops, py_results, strict=True):
                self.run_read(py_result, o)

        for o in write_ops:
            self.run_write(o)

    def run_read(self, py_result: dict, o: dict):
        op, args, op_id = o["op"], o.get("args", {}), o.get("id", "?")
        py_out = py_result["out"]
        self.timings[op]["py"].append(py_result["dt"])

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
        elif not py_changed and not rs_changed:
            # agreeing refusals, but no write path was exercised
            self.verdict("both_refused", op_id, op)
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
        tx_backed = op in {"rename-symbol", "replace-symbol"}
        if tx_backed and py_changed:
            sh(ref_symbol("undo"), cwd=py_copy)
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
        # The pid keeps concurrent runs on same-named corpora apart.
        dst = Path(f"/tmp/symdiff-{self.target.name}-{tag}-{os.getpid()}-{self._copy_seq}")
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


# ---------------------------------------------------------- MCP mode

MCP_PROTOCOL = "2025-06-18"
MCP_TIMEOUT_S = 600


class McpServer:
    """One `symbol mcp` process driven over newline-delimited JSON-RPC."""

    def __init__(self, cmd: list[str], root: Path):
        self.root = root
        self.proc = subprocess.Popen(
            cmd, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        self.seq = 0
        self.initialize = self.request(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
             "clientInfo": {"name": "diff_runner", "version": "0"}},
        )
        self.notify("notifications/initialized")

    def notify(self, method: str):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.seq += 1
        msg = {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        ready, _, _ = select.select([self.proc.stdout], [], [], MCP_TIMEOUT_S)
        line = self.proc.stdout.readline() if ready else ""
        if not line:
            raise RuntimeError(f"no reply to {method} (exit={self.proc.poll()})")
        reply = json.loads(line)
        if "error" in reply:
            return {"rpc_error": reply["error"]}
        return reply["result"]

    def call(self, tool: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()


def py_mcp(root: Path) -> McpServer:
    return McpServer(ref_symbol("mcp", "--root", str(root)), root)


def rs_mcp(root: Path) -> McpServer:
    return McpServer([str(RUST_BIN), "--root", str(root), "mcp"], root)


def mcp_read_calls(op: str, args: dict) -> list[tuple[str, dict]]:
    if op == "code":
        target = args["target"]
        return [
            ("SymbolBody", {"target": target}),
            ("SymbolBody", {"target": target, "include_refs": True, "offset": 1, "limit": 3}),
        ]
    if op == "callers":
        return [("SymbolCallers", {"name": args["name"]})]
    if op == "outline":
        return [("SymbolOutline", {"target": args["query"]})]
    if op == "search":
        return [("SearchSymbol", {
            "patterns": args.get("patterns", []), "kind": args.get("kind"),
            "file": args.get("file"), "regex": bool(args.get("regex")),
            "fixed": bool(args.get("fixed")), "ignore_case": bool(args.get("ignore_case")),
            "limit": int(args.get("limit", 100)),
        })]
    raise ValueError(op)


def mcp_write_call(op: str, args: dict) -> tuple[str, dict]:
    if op == "rename-symbol":
        return "RenameSymbol", {"target": args["qualified_path"], "new_name": args["new_name"]}
    if op == "replace-symbol":
        return "ReplaceSymbol", {"target": args["qualified_path"], "content": args["content"]}
    if op == "delete-symbol":
        return "DeleteSymbol", {"target": args["qualified_path"], "force": bool(args.get("force"))}
    if op == "insert-symbol":
        return "InsertSymbol", {
            "target": args["anchor_path"], "position": args["position"],
            "content": args.get("content", ""), "reindent": not args.get("no_reindent"),
        }
    if op == "patch":
        # No force: the first call must come back needs_read_confirmation
        # and the retry must pass — the session read-cache contract.
        return "Patch", {"file": args["file"], "range": args["range"],
                         "content": args.get("content", "")}
    raise ValueError(op)


def mcp_normalize(result, root: Path):
    """Replace the copy's root in every string; mask transaction ids."""
    roots = sorted({str(root), str(root.resolve()), os.path.realpath(root)}, key=len, reverse=True)

    def fix(v):
        if isinstance(v, str):
            for r in roots:
                v = v.replace(r, "<root>")
            return v
        if isinstance(v, list):
            return [fix(x) for x in v]
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        return v

    result = fix(result)
    for block in result.get("content", []) if isinstance(result, dict) else []:
        text = block.get("text", "")
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("transaction_id"):
                payload["transaction_id"] = "<tx>"
                # Re-serialize with the same layout both servers use.
                block["text"] = json.dumps(payload, indent=2, ensure_ascii=False)
    return result


def mcp_sort_hits(result):
    """SearchSymbol payload with `hits` in a canonical order."""
    for block in result.get("content", []) if isinstance(result, dict) else []:
        try:
            payload = json.loads(block.get("text", ""))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("hits"), list):
            payload["hits"] = sorted(payload["hits"], key=lambda h: json.dumps(h, sort_keys=True))
            block["text"] = json.dumps(payload, indent=2, ensure_ascii=False)
    return result


def strip_affected(text: str) -> str:
    """Rename text without its 'affects N interface contract(s)' block."""
    out, skipping = [], False
    for line in text.split("\n"):
        if line.startswith("affects ") and "interface contract(s)" in line:
            skipping = True
            if out and out[-1] == "":
                out.pop()
            continue
        if skipping and line.startswith("  "):
            continue
        skipping = False
        out.append(line)
    return "\n".join(out)


def mcp_text(result) -> str:
    if not isinstance(result, dict):
        return json.dumps(result)
    parts = [b.get("text", "") for b in result.get("content", [])]
    return "\n".join(parts) if parts else json.dumps(result, indent=2)


def mcp_diff(a, b) -> str:
    lines = difflib.unified_diff(
        mcp_text(a).splitlines(), mcp_text(b).splitlines(), "py", "rs", lineterm="", n=1,
    )
    return "\n".join(list(lines)[:40]) or f"envelope py={a} rs={b}"


class McpRunner(Runner):
    """Dataset replay against both MCP servers. Reads share one server pair;
    every write gets fresh copies and a fresh pair, and runs dry-run → apply
    → follow-up reads (outline of touched files) → Undo → Refresh."""

    def verdict(self, kind, op_id, op, detail=""):
        self.verdicts[kind] += 1
        if kind == "MISMATCH":
            self.failures.append({"id": op_id, "op": op, "detail": detail[:3000]})

    def compare(self, op_id, op, py_out, rs_out, py_root, rs_root, unordered_hits=False):
        a, b = mcp_normalize(py_out, py_root), mcp_normalize(rs_out, rs_root)
        if unordered_hits:
            a, b = mcp_sort_hits(a), mcp_sort_hits(b)
        if a == b:
            self.verdict("match", op_id, op)
        elif op == "RenameSymbol" and mcp_text(a) == strip_affected(mcp_text(b)):
            # The Python Go adapter pops its affected-interfaces cache on the
            # first call and serves later identical renames from a cached
            # analysis, so a repeat in one session omits the block.
            self.verdict("known_divergence", op_id, op, "python drops affected interfaces on repeat")
        else:
            self.verdict("MISMATCH", op_id, op, mcp_diff(a, b))
        return a == b

    def report(self):
        out = super().report()
        out["failures"] = self.failures[:500]
        return out

    def timed_call(self, server: McpServer, side: str, tool: str, arguments: dict):
        t0 = time.perf_counter()
        out = server.call(tool, arguments)
        self.timings[tool][side].append(time.perf_counter() - t0)
        return out

    def pair_call(self, op_id, py, rs, tool, arguments, unordered_hits=False):
        py_out = self.timed_call(py, "py", tool, arguments)
        rs_out = self.timed_call(rs, "rs", tool, arguments)
        self.compare(op_id, tool, py_out, rs_out, py.root, rs.root, unordered_hits)
        return py_out, rs_out

    def tools_check(self):
        py_copy, rs_copy = self.fresh_copy("toolspy"), self.fresh_copy("toolsrs")
        py, rs = py_mcp(py_copy), rs_mcp(rs_copy)
        try:
            a = dict(py.initialize, serverInfo={"name": py.initialize["serverInfo"]["name"]})
            b = dict(rs.initialize, serverInfo={"name": rs.initialize["serverInfo"]["name"]})
            self.compare("initialize", "initialize", a, b, py_copy, rs_copy)
            py_tools = py.request("tools/list", {}).get("tools", [])
            rs_tools = rs.request("tools/list", {}).get("tools", [])
            pt = {t["name"]: t for t in py_tools}
            self.rust_only = [t["name"] for t in rs_tools if t["name"] not in pt]
            for name in self.rust_only:
                self.verdict("rust_only", f"tools/list:{name}", "tools/list")
            rs_tools = [t for t in rs_tools if t["name"] in pt]
            if py_tools == rs_tools:
                self.verdict("match", "tools/list", "tools/list")
            else:
                rt = {t["name"]: t for t in rs_tools}
                for name in sorted(set(pt) | set(rt)):
                    if pt.get(name) != rt.get(name):
                        self.verdict("MISMATCH", f"tools/list:{name}", "tools/list",
                                     f"py={json.dumps(pt.get(name))[:240]} rs={json.dumps(rt.get(name))[:240]}")
                if [t["name"] for t in py_tools] != [t["name"] for t in rs_tools]:
                    self.verdict("MISMATCH", "tools/list:order", "tools/list", "tool order differs")
        finally:
            py.close()
            rs.close()
            shutil.rmtree(py_copy, ignore_errors=True)
            shutil.rmtree(rs_copy, ignore_errors=True)

    def run_ops(self, ops: list[dict]):
        read_ops = [o for o in ops if o["op"] in READ_OPS]
        write_ops = [o for o in ops if o["op"] in WRITE_OPS]
        for o in ops:
            if o["op"] not in READ_OPS | WRITE_OPS:
                self.verdict("MISMATCH", o.get("id", "?"), o["op"], "unknown op")

        if read_ops:
            py_copy, rs_copy = self.fresh_copy("mreadpy"), self.fresh_copy("mreadrs")
            py, rs = py_mcp(py_copy), rs_mcp(rs_copy)
            try:
                for o in read_ops:
                    for i, (tool, arguments) in enumerate(mcp_read_calls(o["op"], o.get("args", {}))):
                        self.pair_call(f"{o.get('id', '?')}/{i}", py, rs, tool, arguments)
            finally:
                py.close()
                rs.close()
                shutil.rmtree(py_copy, ignore_errors=True)
                shutil.rmtree(rs_copy, ignore_errors=True)

        for o in write_ops:
            self.run_write(o)
            if o["op"] == "patch" and not o.get("expect_error"):
                self.run_multi_patch(o)

    def with_pair(self, tag, body):
        py_copy, rs_copy = self.fresh_copy(f"{tag}py"), self.fresh_copy(f"{tag}rs")
        pristine = tree_hash(py_copy)
        py = rs = None
        try:
            py, rs = py_mcp(py_copy), rs_mcp(rs_copy)
            body(py, rs, pristine)
        finally:
            for server in (py, rs):
                if server is not None:
                    server.close()
            shutil.rmtree(py_copy, ignore_errors=True)
            shutil.rmtree(rs_copy, ignore_errors=True)

    def tree_verdict(self, o, py_root, rs_root, pristine):
        op, op_id = o["op"], o.get("id", "?")
        after_py, after_rs = tree_hash(py_root), tree_hash(rs_root)
        py_changed, rs_changed = after_py != pristine, after_rs != pristine
        if o.get("expect_error"):
            ok = not py_changed and not rs_changed
            self.verdict("match" if ok else "MISMATCH", op_id, op,
                         "" if ok else f"expected refusal; py_changed={py_changed} rs_changed={rs_changed}")
        elif not py_changed and not rs_changed:
            self.verdict("both_refused", op_id, op)
        elif after_py == after_rs:
            self.verdict("match", op_id, op)
        else:
            diff_files = sorted(k for k in set(after_py) | set(after_rs)
                                if after_py.get(k) != after_rs.get(k))
            kind = "known_divergence" if ("engine-kind" in set(o.get("tags", []))
                                          and op == "rename-symbol") else "MISMATCH"
            self.verdict(kind, op_id, op, f"trees differ: {diff_files[:6]}")
        changed = sorted(k for k in set(after_py) | set(pristine)
                         if after_py.get(k) != pristine.get(k))
        return py_changed or rs_changed, changed

    def run_write(self, o: dict):
        op, args, op_id = o["op"], o.get("args", {}), o.get("id", "?")
        tool, arguments = mcp_write_call(op, args)

        def body(py, rs, pristine):
            if op == "patch":
                steps = [("needs-read", {**arguments, "dry_run": True}),
                         ("dry", {**arguments, "dry_run": True}),
                         ("apply", arguments)]
            else:
                steps = [("dry", {**arguments, "dry_run": True}), ("apply", arguments)]
            for label, a in steps:
                self.pair_call(f"{op_id}/{label}", py, rs, tool, a)
            changed, files = self.tree_verdict(o, py.root, rs.root, pristine)
            if not changed:
                return
            # The in-RAM index must reflect the write on both sides.
            for f in [f for f in files if (py.root / f).is_file()][:3]:
                self.pair_call(f"{op_id}/outline:{f}", py, rs, "SymbolOutline", {"target": f})
            # Hit order after a multi-file refresh follows set iteration on
            # both sides (hash-randomized in Python), so compare as a set.
            if op == "rename-symbol":
                self.pair_call(f"{op_id}/search-new", py, rs, "SearchSymbol",
                               {"patterns": [args["new_name"]]}, unordered_hits=True)
            if op in {"rename-symbol", "replace-symbol"}:
                self.pair_call(f"{op_id}/undo", py, rs, "Undo", {})
                for side, server in (("py", py), ("rs", rs)):
                    ok = tree_hash(server.root) == pristine
                    self.verdict("match" if ok else "MISMATCH", f"{op_id}/undo-{side}", op,
                                 f"{side} undo not pristine")
                leaf = args["qualified_path"].rsplit(".", 1)[-1]
                self.pair_call(f"{op_id}/search-old", py, rs, "SearchSymbol",
                               {"patterns": [leaf]}, unordered_hits=True)
            self.pair_call(f"{op_id}/refresh", py, rs, "Refresh", {})

        self.with_pair("mw", body)

    def run_multi_patch(self, o: dict):
        """MultiPatch twins of a patch op: range-addressed (needs a read,
        then passes on retry), then an `old`-addressed edit of the bytes it
        wrote (no read needed)."""
        args, op_id = o.get("args", {}), o.get("id", "?")
        edit = {"range": args["range"], "content": args.get("content", "")}

        def body(py, rs, pristine):
            call = {"file": args["file"], "edits": [edit]}
            self.pair_call(f"{op_id}/mp-needs-read", py, rs, "MultiPatch", call)
            py_out, _ = self.pair_call(f"{op_id}/mp-range", py, rs, "MultiPatch", call)
            self.tree_verdict({**o, "id": f"{op_id}/mp-tree"}, py.root, rs.root, pristine)
            written = edit["content"]
            if written and "status: applied" in mcp_text(py_out):
                self.pair_call(f"{op_id}/mp-old", py, rs, "MultiPatch",
                               {"file": args["file"],
                                "edits": [{"old": written, "content": "# rewritten\n"}]})
                ok = tree_hash(py.root) == tree_hash(rs.root)
                self.verdict("match" if ok else "MISMATCH", f"{op_id}/mp-old-tree", "MultiPatch",
                             "" if ok else "trees differ after old-addressed edit")
            self.pair_call(f"{op_id}/mp-outline", py, rs, "SymbolOutline", {"target": args["file"]})

        self.with_pair("mmp", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("ops", nargs="?", help="ops dataset (JSONL)")
    ap.add_argument("--index-check", action="store_true")
    ap.add_argument("--mcp", action="store_true",
                    help="compare the MCP servers (tools/list + dataset as tool calls)")
    ap.add_argument("--reference", default=os.environ.get("SYMBOL_REFERENCE"),
                    help="checkout holding the Python implementation (default: $SYMBOL_REFERENCE)")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    if not args.reference or not (Path(args.reference) / "src/wyolet/symbol/shared/symbol_index.py").is_file():
        ap.error("--reference must be a checkout holding the Python implementation "
                 "(setup: scripts/datasets/README.md)")
    global REFERENCE
    REFERENCE = Path(args.reference).resolve()
    # The reference's `symbol` must run its Python implementation, not delegate.
    os.environ["SYMBOL_IMPL"] = "python"

    if args.mcp:
        runner = McpRunner(Path(args.target).resolve())
        runner.tools_check()
    else:
        runner = Runner(Path(args.target).resolve())
    if not args.mcp and (args.index_check or not args.ops):
        runner.index_check()
    if args.ops:
        ops = [json.loads(l) for l in Path(args.ops).read_text().splitlines() if l.strip()]
        runner.run_ops(ops)

    report = runner.report()
    if hasattr(runner, "read_copy"):
        shutil.rmtree(runner.read_copy, ignore_errors=True)
    print(json.dumps({"verdicts": report["verdicts"],
                      "timing_median_s": report["timing_median_s"]}, indent=2))
    if getattr(runner, "rust_only", None):
        print(f"rust-only tools: {', '.join(runner.rust_only)}")
    for f in report["failures"][:15]:
        print(f"MISMATCH {f['id']} ({f['op']}): {f['detail'][:220]}")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, default=str))
        print(f"report → {args.report}")
    sys.exit(1 if runner.verdicts.get("MISMATCH") else 0)


if __name__ == "__main__":
    main()
