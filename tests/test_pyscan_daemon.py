"""pyscan daemon: rename analysis asks the client index questions mid-call.

Drives `python -m wyolet.symbol.adapters.pyscan` over stdio and answers its
`index/*` requests from a canned table, the way the Rust core does.
"""

import json
import subprocess
import sys
from pathlib import Path


def _session(project: Path, index_answers: dict):
    proc = subprocess.Popen(
        [sys.executable, "-m", "wyolet.symbol.adapters.pyscan"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    asked: list[tuple[str, dict]] = []
    seq = 0

    def call(method: str, params: dict):
        nonlocal seq
        seq += 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while True:
            msg = json.loads(proc.stdout.readline())
            if "method" in msg:
                asked.append((msg["method"], msg["params"]))
                key = next(iter(msg["params"].values()))
                result = index_answers[msg["method"]](key)
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
                proc.stdin.flush()
                continue
            assert msg["id"] == seq
            return msg

    call("initialize", {"protocol_version": "1", "project_root": str(project)})
    return proc, call, asked


def test_member_rename_resolves_receivers_through_client_queries(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n"
        "    def save(self): return 1\n"
        "class B:\n"
        "    def save(self): return 2\n"
        "def run(a: A, b: B):\n"
        "    a.save()\n"
        "    b.save()\n"
    )
    decls = {"m.A": "class", "m.B": "class", "m.A.save": "method", "m.B.save": "method"}
    answers = {
        "index/owners_of_leaf": lambda leaf: ["m.A.save", "m.B.save"],
        "index/class_bases": lambda qpath: [],
        "index/find_declaration": lambda qpath: (
            {"path": str(tmp_path / "m.py"), "file": "m.py", "byte_range": [0, 1], "kind": decls[qpath]}
            if qpath in decls else None
        ),
    }
    proc, call, asked = _session(tmp_path, answers)
    try:
        resp = call("rename_member", {
            "path": str(tmp_path / "m.py"), "leaf": "save", "target_qpath": "m.A.save",
            "target_owner_qpath": "m.A", "is_declaring_file": False, "new_name": "persist",
        })
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    result = resp["result"]
    assert [(r["line"], r["receiver_source"]) for r in result["rewrites"]] == [(6, "a")]
    assert [(s["line"], s["resolved_to_qpath"]) for s in result["skipped_mismatch"]] == [(7, "m.B")]
    methods = {m for m, _ in asked}
    assert methods == {"index/owners_of_leaf", "index/find_declaration", "index/class_bases"}
    # Answers are memoized within one request.
    assert len(asked) == len({(m, json.dumps(p, sort_keys=True)) for m, p in asked})


def test_index_query_error_fails_the_request_not_the_daemon(tmp_path):
    (tmp_path / "m.py").write_text("class A:\n    def save(self): return 1\n")
    proc, call, _ = _session(tmp_path, {})
    try:
        # Answer the daemon's query with a JSON-RPC error by hand.
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 99, "method": "rename_member", "params": {
                "path": str(tmp_path / "m.py"), "leaf": "save", "target_qpath": "m.A.save",
                "target_owner_qpath": "m.A", "is_declaring_file": False, "new_name": "persist",
            },
        }) + "\n")
        proc.stdin.flush()
        query = json.loads(proc.stdout.readline())
        assert query["method"] == "index/owners_of_leaf"
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": query["id"], "error": {"code": -32603, "message": "no index"},
        }) + "\n")
        proc.stdin.flush()
        failed = json.loads(proc.stdout.readline())
        assert failed["id"] == 99 and "no index" in failed["error"]["message"]
        alive = call("signature", {"text": "def f(x): pass\n"})
        assert alive["result"]["signature"] == "def f(x)"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
