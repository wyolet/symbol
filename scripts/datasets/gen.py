"""Author differential-test JSONL datasets from REAL index dumps.
Every target is drawn from enum-<corpus>.json / sigs-<corpus>.json — nothing invented.
Usage: python gen.py <corpus> <enum.json> <sigs.json> <out.jsonl>
"""
import json, sys
from collections import defaultdict

corpus, enum_path, sigs_path, out_path = sys.argv[1:5]
enum = json.loads(open(enum_path).read())
sigs = json.loads(open(sigs_path).read())
rows = enum["rows"]
by_kind = defaultdict(list)
for r in rows:
    by_kind[r["kind"]].append(r)
qpaths = {r["qpath"] for r in rows}
ambig = enum["ambiguous"]           # qpath -> n (>1)
cc = enum["caller_counts"]          # leaf -> caller count

ENGINE_KINDS_PY = {"method", "async_method", "function", "async_function", "class", "constant"}
ENGINE_KINDS_GO = {"type", "var", "const", "function", "method"}
ENGINE_KINDS = ENGINE_KINDS_GO if corpus == "gin" else ENGINE_KINDS_PY

ops = []
n = defaultdict(int)
_PREFIX = {"code": "cd", "callers": "kl", "outline": "ol", "search": "sr",
           "rename-symbol": "rn", "rename-parameter": "rp", "replace-symbol": "rs",
           "insert-symbol": "is", "delete-symbol": "ds", "patch": "pt"}
def emit(op, args, **extra):
    n[op] += 1
    d = {"id": f"{_PREFIX[op]}{n[op]:03d}", "op": op, "args": args}
    d.update(extra)
    ops.append(d)

def rn_tag(kind):
    return {"tags": ["engine-kind"]} if kind in ENGINE_KINDS else {}

# ---------------- READS (~70%) ----------------
# code by full qpath across kinds
sample = []
for k, lst in by_kind.items():
    sample += lst[:14]
for r in sample:
    emit("code", {"target": r["qpath"]})
# code by file:line range (real ranges)
for r in sample[::3][:30]:
    a, b = r["range"]
    emit("code", {"target": f"{r['file']}:{a}-{b}"})
# code by bare-leaf / dotted-suffix (suffix addressing)
for r in sample[::5][:30]:
    leaf = r["qpath"].split(".")[-1]
    emit("code", {"target": leaf})
    parts = r["qpath"].split(".")
    if len(parts) >= 2:
        emit("code", {"target": ".".join(parts[-2:])})
# code on AMBIGUOUS qpaths (multi-row → candidates both sides)
for q in list(ambig)[:15]:
    emit("code", {"target": q})
# callers: hot, mid, zero
leaves_sorted = sorted(cc.items(), key=lambda x: -x[1])
for leaf, _ in leaves_sorted[:12]:              # hot
    emit("callers", {"name": leaf})
mid = [l for l, c in cc.items() if 1 <= c <= 5][:12]
for leaf in mid:
    emit("callers", {"name": leaf})
zero = [l for l, c in cc.items() if c == 0][:10]
for leaf in zero:
    emit("callers", {"name": leaf})
# callers by dotted path (leaf extracted)
for r in sample[::7][:10]:
    emit("callers", {"name": r["qpath"]})
# outline by file
files = sorted({r["file"] for r in rows})
for f in files[:35]:
    emit("outline", {"query": f})
# outline by symbol path (classes/types)
for r in (by_kind.get("class", []) + by_kind.get("type", []))[:20]:
    emit("outline", {"query": r["qpath"]})
# search: single/multi patterns, kind filters, file scope, flags
search_terms = [r["qpath"].split(".")[-1] for r in sample[::4]][:20]
for t in search_terms:
    emit("search", {"patterns": [t]})
for t in search_terms[:8]:
    emit("search", {"patterns": [t], "ignore_case": True})
for t in search_terms[:6]:
    emit("search", {"patterns": [t], "fixed": True})
for k in by_kind:
    emit("search", {"patterns": [search_terms[0]], "kind": k, "limit": 50})
for f in files[:6]:
    emit("search", {"patterns": [search_terms[0]], "file": f})
# multi-pattern AND
for a, b in zip(search_terms[::2], search_terms[1::2]):
    emit("search", {"patterns": [a, b]})
# regex
emit("search", {"patterns": ["get.*"], "regex": True, "limit": 30})
emit("search", {"patterns": ["^__"], "regex": True, "limit": 30})

read_count = len(ops)

# ---------------- WRITES (~30%) ----------------
FRESH = "_wp7"
def fresh(leaf):
    return leaf + FRESH

# rename-symbol across kinds (undoable; engine-kind tagged where applicable)
def pick(kind, k):
    return [r for r in by_kind.get(kind, []) if r["qpath"] in qpaths][:k]

CURATED = json.loads(open(f"{enum_path}.curated").read()) if False else {}

# corpus-specific curated write targets (verified real)
if corpus == "flask":
    # field renames with same-name decoys on OTHER classes
    for q in ["flask.json.tag.JSONTag.key", "flask.views.View.methods",
              "flask.sansio.blueprints.Blueprint.name", "flask.cli.ScriptInfo.data",
              "flask.sansio.scaffold.Scaffold.cli", "flask.sansio.blueprints.Blueprint.url_prefix",
              "flask.app.Flask.session_interface"]:
        leaf = q.split(".")[-1]
        emit("rename-symbol", {"qualified_path": q, "new_name": fresh(leaf)})
    # constant renames (import/ref sites) — engine-kind
    for q in ["flask.config.T", "flask.ctx.F", "flask.app.F", "flask.views.F"]:
        emit("rename-symbol", {"qualified_path": q, "new_name": fresh(q.split(".")[-1])},
             **rn_tag("constant"))
    # method renames across inheritance / MRO / super()
    for q in ["flask.views.View.dispatch_request", "flask.views.View.as_view",
              "flask.sansio.scaffold.Scaffold.add_url_rule",
              "flask.json.tag.JSONTag.to_json", "flask.json.tag.JSONTag.to_python",
              "flask.json.provider.JSONProvider.loads",
              "flask.app.Flask.get_send_file_max_age"]:
        emit("rename-symbol", {"qualified_path": q, "new_name": fresh(q.split(".")[-1])},
             **rn_tag("method"))
    # function + class renames — engine-kind
    for q in ["flask.logging.create_logger", "flask.helpers.get_root_path",
              "flask.logging.has_level_handler", "flask.json.provider._default"]:
        emit("rename-symbol", {"qualified_path": q, "new_name": fresh(q.split(".")[-1])},
             **rn_tag("function"))
    for q in ["flask.json.tag.TagUUID", "flask.templating.DispatchingJinjaLoader",
              "flask.sessions.NullSession"]:
        emit("rename-symbol", {"qualified_path": q, "new_name": fresh(q.split(".")[-1])},
             **rn_tag("class"))
    # bulk CLEAN renames (undoable) — real flask.* rows, fresh non-colliding names,
    # lifts the write ratio toward 30% without adding non-undoable/patch noise
    _done = {o["args"].get("qualified_path") for o in ops if o["op"] == "rename-symbol"}
    for kind in ("function", "method", "field"):
        picked = 0
        for r in by_kind.get(kind, []):
            if picked >= 15:
                break
            q = r["qpath"]
            if not q.startswith("flask.") or q in _done or q.endswith(".F"):
                continue
            emit("rename-symbol", {"qualified_path": q, "new_name": r["leaf"] + FRESH},
                 **rn_tag(kind))
            _done.add(q)
            picked += 1
    # replace-symbol (engine-kind)
    emit("replace-symbol", {"qualified_path": "flask.helpers.get_load_dotenv",
         "content": "def get_load_dotenv(default: bool = True) -> bool:\n    return bool(default)\n"},
         **rn_tag("function"))
    emit("replace-symbol", {"qualified_path": "flask.logging.has_level_handler",
         "content": "def has_level_handler(logger: logging.Logger) -> bool:\n    return True\n"},
         **rn_tag("function"))
    # rename-parameter (real params; several with real kwarg call sites)
    for fq, p in [("flask.logging.create_logger", "app"),
                  ("flask.ctx.AppContext.pop", "exc"),
                  ("flask.app.Flask.create_url_adapter", "request"),
                  ("flask.helpers.get_load_dotenv", "default"),
                  ("flask.json.provider._default", "o"),
                  ("flask.logging.has_level_handler", "logger"),
                  ("flask.sessions.SecureCookieSessionInterface.get_signing_serializer", "app"),
                  ("flask.helpers.get_root_path", "import_name"),
                  ("flask.templating.DispatchingJinjaLoader._iter_loaders", "template"),
                  ("flask.sansio.app.App.trap_http_exception", "e")]:
        emit("rename-parameter", {"func_qpath": fq, "param": p, "new_name": p + FRESH})
    # insert-symbol — all four positions, incl decorated anchors
    for anchor, pos in [("flask.logging.create_logger", "after"),
                        ("flask.logging.create_logger", "before"),
                        ("flask.json.provider.DefaultJSONProvider", "start"),
                        ("flask.json.provider.DefaultJSONProvider", "end"),
                        ("flask.views.View.dispatch_request", "after"),
                        ("flask.cli.routes_command", "before"),
                        ("flask.helpers.get_root_path", "after")]:
        emit("insert-symbol", {"anchor_path": anchor, "position": pos,
                               "content": "# wp7 inserted marker\n"})
    emit("insert-symbol", {"anchor_path": "flask.helpers.get_root_path", "position": "after",
                           "content": "def _wp7_helper():\n    return 1\n", "no_reindent": True})
    # boundary patches (first line, last line, mid) — real files
    emit("patch", {"file": "src/flask/logging.py", "range": "1-1", "content": "# wp7 header"})
    emit("patch", {"file": "src/flask/logging.py", "range": "2-2", "content": "# second"})
    emit("patch", {"file": "src/flask/globals.py", "range": "1-2", "content": "# top"})
    emit("patch", {"file": "src/flask/__init__.py", "range": "1-1", "content": "# init"})
    emit("patch", {"file": "src/flask/json/tag.py", "range": "3-3", "content": "# tag"})
    # delete-symbol with force (unreferenced constants + a helper)
    for q in ["flask.config.T", "flask.ctx.F", "flask.app.F", "flask.sansio.scaffold.F"]:
        emit("delete-symbol", {"qualified_path": q, "force": True})
    # ------- expect_error: every write op refuses -------
    emit("rename-symbol", {"qualified_path": "flask.views.View.dispatch_request",
         "new_name": "as_view"}, expect_error=True)          # colliding sibling
    emit("rename-symbol", {"qualified_path": "flask.does.not.exist", "new_name": "x"},
         expect_error=True)                                   # nonexistent
    emit("delete-symbol", {"qualified_path": "flask.logging.create_logger"},
         expect_error=True)                                   # referenced, no force
    emit("delete-symbol", {"qualified_path": "flask.no.such.symbol", "force": True},
         expect_error=True)                                   # nonexistent
    emit("replace-symbol", {"qualified_path": "flask.no.such.func", "content": "def x():\n    pass\n"},
         expect_error=True)
    emit("rename-parameter", {"func_qpath": "flask.logging.create_logger", "param": "nope",
         "new_name": "y"}, expect_error=True)                 # no such param
    emit("rename-parameter", {"func_qpath": "flask.cli.routes_command", "param": "sort",
         "new_name": "all_methods"}, expect_error=True)       # collides with sibling param
    emit("insert-symbol", {"anchor_path": "flask.nope.anchor", "position": "after",
         "content": "x"}, expect_error=True)                  # bad anchor
    emit("insert-symbol", {"anchor_path": "flask.logging.create_logger", "position": "sideways",
         "content": "x"}, expect_error=True)                  # bad position
    emit("patch", {"file": "src/flask/logging.py", "range": "999999-999999", "content": "x"},
         expect_error=True)                                   # out of bounds
    emit("patch", {"file": "src/flask/nonexistent_file.py", "range": "1-1", "content": "x"},
         expect_error=True)                                   # missing file
    # code on nonexistent (read expect_error not needed — reads canon compare)
    emit("code", {"target": "flask.totally.made.up.path"})

elif corpus == "gin":
    P = "github.com/gin-gonic/gin"
    B = "github.com/gin-gonic/gin/binding"
    R = "github.com/gin-gonic/gin/render"
    # type renames (engine-kind)
    for q in [f"{P}.Context", f"{P}.RouterGroup", f"{P}.Engine", f"{P}.HandlerFunc",
              f"{B}.Binding", f"{R}.Delims"]:
        if q in qpaths:
            emit("rename-symbol", {"qualified_path": q, "new_name": q.split(".")[-1] + FRESH},
                 **rn_tag("type"))
    # var renames (engine-kind)
    for r in by_kind.get("var", [])[:8]:
        emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH},
             **rn_tag("var"))
    # const renames incl. import/ref sites (engine-kind)
    for r in by_kind.get("const", [])[:10]:
        emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH},
             **rn_tag("const"))
    # method renames — interface-implicating (Bind/Name/Render implement interfaces)
    for q in [f"{B}.queryBinding.Name", f"{B}.headerBinding.Bind", f"{R}.JSON.Render",
              f"{R}.XML.WriteContentType", f"{B}.jsonBinding.BindBody"]:
        if q in qpaths:
            emit("rename-symbol", {"qualified_path": q, "new_name": q.split(".")[-1] + FRESH},
                 **rn_tag("method"))
    # receiver-discrimination: same method leaf on different receivers — rename ONE
    for q in [f"{P}.responseWriter.Write", f"{R}.failWriter.Write"]:
        if q in qpaths:
            emit("rename-symbol", {"qualified_path": q, "new_name": "Write" + FRESH},
                 **rn_tag("method"))
    # function renames (engine-kind)
    for r in by_kind.get("function", []):
        if r["qpath"].startswith(P + ".") and not r["leaf"].startswith("Test") and r["nrefs"] >= 3:
            emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH},
                 **rn_tag("function"))
        if n["rename-symbol"] > 40:
            break
    # bulk CLEAN Go renames (undoable engine renames) — real non-test funcs/methods/types
    _gdone = {o["args"].get("qualified_path") for o in ops if o["op"] == "rename-symbol"}
    for kind in ("function", "method", "type"):
        picked = 0
        for r in by_kind.get(kind, []):
            if picked >= 12:
                break
            q = r["qpath"]
            if q in _gdone or r["leaf"].startswith("Test") or r["leaf"] == "init":
                continue
            emit("rename-symbol", {"qualified_path": q, "new_name": r["leaf"] + FRESH},
                 **rn_tag(kind))
            _gdone.add(q)
            picked += 1
    # replace-symbol
    for r in by_kind.get("function", []):
        if r["qpath"].startswith(P + ".") and r["leaf"].startswith("Test"):
            emit("replace-symbol", {"qualified_path": r["qpath"],
                 "content": f"func {r['leaf']}(t *testing.T) {{\n}}\n"}, **rn_tag("function"))
        if n["replace-symbol"] >= 3:
            break
    # successful insert-symbol on Go anchors (verified identical both sides)
    emit("insert-symbol", {"anchor_path": f"{P}.Version", "position": "after",
                           "content": "// wp7 marker\n"})
    emit("insert-symbol", {"anchor_path": f"{P}.CreateTestContextOnly", "position": "before",
                           "content": "// wp7 before\n"})
    # successful boundary patches on real Go files
    emit("patch", {"file": "version.go", "range": "1-1", "content": "// wp7 hdr"})
    emit("patch", {"file": "utils.go", "range": "1-1", "content": "// wp7 hdr"})
    # successful force-deletes of zero-ref consts
    for q in [f"{P}.dunno", f"{P}.localhostIP"]:
        emit("delete-symbol", {"qualified_path": q, "force": True})
    # ------- expect_error (all forms both refuse) -------
    # same-package type-name collision (both verified to refuse with name_collision)
    emit("rename-symbol", {"qualified_path": f"{P}.Context", "new_name": "Engine"}, expect_error=True)
    emit("rename-symbol", {"qualified_path": f"{P}.NoSuchType", "new_name": "X"}, expect_error=True)
    emit("delete-symbol", {"qualified_path": f"{P}.Context"}, expect_error=True)   # referenced
    emit("delete-symbol", {"qualified_path": f"{P}.NoSuch", "force": True}, expect_error=True)
    emit("replace-symbol", {"qualified_path": f"{P}.NoSuch", "content": "func X(){}\n"}, expect_error=True)
    emit("patch", {"file": "gin.go", "range": "999999-999999", "content": "x"}, expect_error=True)
    emit("insert-symbol", {"anchor_path": f"{P}.NoSuch", "position": "after", "content": "x"}, expect_error=True)

elif corpus == "self":
    # reads-heavy already; clean undoable writes + expect_error
    S = "wyolet.symbol"
    # rename-symbol functions (engine-kind), pick real low-risk internal funcs
    cand = [r for r in by_kind.get("function", [])
            if r["qpath"].startswith(S) and r["leaf"].startswith("_") and 1 <= r["nrefs"] <= 40][:14]
    for r in cand:
        emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH},
             **rn_tag("function"))
    # method renames (engine-kind)
    for r in by_kind.get("method", []):
        if r["qpath"].startswith(S) and r["leaf"].startswith("_") and r["nrefs"] >= 2:
            emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH},
                 **rn_tag("method"))
        if n["rename-symbol"] > 24:
            break
    # class renames (engine-kind)
    for r in by_kind.get("class", [])[:6]:
        if r["qpath"].startswith(S):
            emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH},
                 **rn_tag("class"))
    # constant renames (engine-kind)
    for r in by_kind.get("constant", []):
        if r["qpath"].startswith(S) and r["leaf"].isupper():
            emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH},
                 **rn_tag("constant"))
        if n["rename-symbol"] > 34:
            break
    # field renames (NOT engine-kind → hard equality on the new field engine)
    for r in by_kind.get("field", []):
        if r["qpath"].startswith(S):
            emit("rename-symbol", {"qualified_path": r["qpath"], "new_name": r["leaf"] + FRESH})
        if n["rename-symbol"] > 44:
            break
    # rename-parameter on real single-line-signature funcs
    added = 0
    for s in sigs:
        if added >= 10:
            break
        if s["qpath"].startswith(S) and s["params"] and "(" in s["sig"] and s["sig"].count("\n") == 0:
            p = s["params"][0]
            if p.isidentifier():
                emit("rename-parameter", {"func_qpath": s["qpath"], "param": p, "new_name": p + FRESH})
                added += 1
    # successful non-transactional writes (runner skips undo assertion for these)
    ins_fn = next(r for r in by_kind["function"] if r["qpath"].startswith(S) and r["nrefs"] >= 2)
    emit("insert-symbol", {"anchor_path": ins_fn["qpath"], "position": "after",
                           "content": "# wp7 marker\n"})
    cls_row = next(r for r in by_kind.get("class", []) if r["qpath"].startswith(S))
    emit("insert-symbol", {"anchor_path": cls_row["qpath"], "position": "end",
                           "content": "WP7_MARKER = 1\n"})
    pfiles = sorted({r["file"] for r in rows if r["file"].endswith(".py")})
    emit("patch", {"file": pfiles[0], "range": "1-1", "content": "# wp7 hdr"})
    emit("patch", {"file": pfiles[1], "range": "1-1", "content": "# wp7 hdr"})
    zero_fn = next((r for r in by_kind["function"]
                    if r["qpath"].startswith(S) and r["nrefs"] == 0), ins_fn)
    emit("delete-symbol", {"qualified_path": zero_fn["qpath"], "force": True})
    # expect_error forms
    ref_fn = next(r for r in by_kind["function"] if r["qpath"].startswith(S) and r["nrefs"] >= 5)
    emit("delete-symbol", {"qualified_path": ref_fn["qpath"]}, expect_error=True)
    emit("rename-symbol", {"qualified_path": f"{S}.no.such.symbol", "new_name": "x"}, expect_error=True)
    emit("delete-symbol", {"qualified_path": f"{S}.no.such", "force": True}, expect_error=True)
    emit("replace-symbol", {"qualified_path": f"{S}.no.such", "content": "def x():\n    pass\n"}, expect_error=True)
    emit("rename-parameter", {"func_qpath": ref_fn["qpath"], "param": "nope", "new_name": "y"}, expect_error=True)
    emit("insert-symbol", {"anchor_path": f"{S}.no.anchor", "position": "after", "content": "x"}, expect_error=True)

# ---------------- write ----------------
with open(out_path, "w") as fh:
    for o in ops:
        fh.write(json.dumps(o) + "\n")

wc = sum(1 for o in ops if o["op"] in {"code", "callers", "outline", "search"})
ww = len(ops) - wc
comp = defaultdict(int)
for o in ops:
    comp[o["op"]] += 1
err = sum(1 for o in ops if o.get("expect_error"))
print(f"{corpus}: {len(ops)} ops  reads={wc} writes={ww} expect_error={err}")
print("  composition:", dict(comp))
