"""python-scan daemon — the Python adapter behind the go-scan wire contract.

Newline-delimited JSON-RPC 2.0 over stdio, mirroring daemon/go-scan so a
non-Python core (the Rust port) can drive Python analysis through the same
protocol it already speaks to Go. All AST work stays in PythonAstAdapter;
this module is a ~150-line socket.

Methods:
  initialize       {project_root} -> {protocol_version}
  scan_file        {path, module_prefix?} -> FileScan dict
  validate_syntax  {source} -> ParseResult dict
  signature        {text} -> {signature}
  preview          {body, signature, max_lines?} -> {preview}
  rename_member / rename_field / rename_module_binding / rename_parameter -> RenameAnalysis dict
  function_parameters -> {parameters: [str] | null}
  project_dependencies {project_root?} -> {dependencies: [str]}
  shutdown         (notification) -> exits

Rename analysis asks the client index questions mid-call (the IndexQuery
protocol): the daemon writes `index/find_declaration` {qpath},
`index/class_bases` {qpath} or `index/owners_of_leaf` {leaf} requests and
reads each reply before continuing. The daemon never loads the index itself.

Stdlib only, Python 3.10+: the Rust core embeds this module's import closure
and runs it with whatever `python3` is on PATH.

Run: python -m wyolet.symbol.adapters.pyscan
"""

import ast
import json
import sys
from dataclasses import asdict
from pathlib import Path

from wyolet.symbol.adapters.python_ast import PythonAstAdapter

_PROTOCOL_VERSION = "1"


class _ClientIndexQuery:
    """IndexQuery answered by the RPC client over the daemon's own stdio.
    One instance per request: answers are memoized while the index can't change."""

    def __init__(self, stdin, stdout) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._seq = 0
        self._memo: dict[tuple[str, str], object] = {}

    def _ask(self, method: str, key: str, value: str):
        memo_key = (method, value)
        if memo_key in self._memo:
            return self._memo[memo_key]
        self._seq += 1
        req_id = f"q{self._seq}"
        self._stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id, "method": method, "params": {key: value},
        }) + "\n")
        self._stdout.flush()
        line = self._stdin.readline()
        if not line:
            raise RuntimeError(f"client closed stdin during {method}")
        reply = json.loads(line)
        if reply.get("id") != req_id:
            raise RuntimeError(f"{method}: reply id {reply.get('id')!r} != {req_id!r}")
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        result = reply.get("result")
        self._memo[memo_key] = result
        return result

    def find_declaration(self, qpath):
        found = self._ask("index/find_declaration", "qpath", qpath)
        if found is None:
            return None
        return Path(found["path"]), found["file"], tuple(found["byte_range"]), found["kind"]

    def class_bases(self, class_qpath):
        return tuple(self._ask("index/class_bases", "qpath", class_qpath))

    def owners_of_leaf(self, leaf):
        return tuple(self._ask("index/owners_of_leaf", "leaf", leaf))


class _Daemon:
    def __init__(self, stdin=None, stdout=None) -> None:
        self.adapter = PythonAstAdapter()
        self.project_root: Path | None = None
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    # ---------------------------------------------------------- methods

    def initialize(self, params: dict) -> dict:
        root = params.get("project_root")
        if root:
            self.project_root = Path(root)
        return {"protocol_version": _PROTOCOL_VERSION}

    def scan_file(self, params: dict) -> dict:
        path = Path(params["path"])
        source = path.read_bytes()
        module_prefix = params.get("module_prefix")
        if module_prefix is None and self.project_root is not None:
            module_prefix = self.adapter.module_prefix(path, self.project_root)
        scan = self.adapter.scan_file(path, source, module_prefix=module_prefix or "")
        return asdict(scan)

    def validate_syntax(self, params: dict) -> dict:
        source = params["source"].encode("utf-8")
        return asdict(self.adapter.validate_syntax(source))

    def signature(self, params: dict) -> dict:
        return {"signature": self.adapter.signature(params["text"])}

    def preview(self, params: dict) -> dict:
        return {
            "preview": self.adapter.preview(
                params["body"],
                params["signature"],
                max_lines=int(params.get("max_lines", 3)),
            )
        }

    # ------------------------------------------------------ v2 rename engine
    # Real receiver-aware rename analysis, so a non-Python caller gets
    # engine-quality renames, not tier-1 regex. Index lookups go back to
    # the caller (see module docstring).

    def _index_query(self) -> _ClientIndexQuery:
        assert self.project_root is not None, "initialize with project_root first"
        return _ClientIndexQuery(self.stdin, self.stdout)

    def _rename_common(self, params: dict) -> dict:
        path = Path(params["path"])
        decl = params.get("decl_byte_range")
        return {
            "path": path,
            "project_root": self.project_root,
            "source": path.read_bytes(),
            "leaf": params["leaf"],
            "target_qpath": params["target_qpath"],
            "index": self._index_query(),
            "is_declaring_file": bool(params["is_declaring_file"]),
            "decl_byte_range": tuple(decl) if decl else None,
            "new_name": params["new_name"],
        }

    def rename_member(self, params: dict) -> dict:
        kwargs = self._rename_common(params)
        kwargs["target_owner_qpath"] = params["target_owner_qpath"]
        return asdict(self.adapter.rename_member(**kwargs))

    def rename_field(self, params: dict) -> dict:
        kwargs = self._rename_common(params)
        kwargs["target_owner_qpath"] = params["target_owner_qpath"]
        return asdict(self.adapter.rename_field(**kwargs))

    def rename_module_binding(self, params: dict) -> dict:
        kwargs = self._rename_common(params)
        kwargs["target_module_qpath"] = params["target_module_qpath"]
        return asdict(self.adapter.rename_module_binding(**kwargs))

    def rename_parameter(self, params: dict) -> dict:
        path = Path(params["path"])
        decl = params.get("decl_byte_range")
        # `source` override: replace-symbol runs kwarg analysis against
        # POST-splice content that isn't on disk yet. When absent, read the
        # file (the standalone per-file analysis path).
        override = params.get("source")
        source = override.encode("utf-8") if override is not None else path.read_bytes()
        return asdict(self.adapter.rename_parameter(
            path=path,
            project_root=self.project_root,
            source=source,
            func_qpath=params["func_qpath"],
            func_leaf=params["func_leaf"],
            param=params["param"],
            index=self._index_query(),
            is_declaring_file=bool(params["is_declaring_file"]),
            decl_byte_range=tuple(decl) if decl else None,
            new_name=params["new_name"],
            kwargs_only=bool(params.get("kwargs_only", False)),
        ))

    def function_parameters(self, params: dict) -> dict:
        path = Path(params["path"])
        decl = params.get("decl_byte_range")
        # `source` override: replace-symbol extracts params from a sliced def
        # and from new content that isn't on disk. When absent, read the file.
        override = params.get("source")
        source = override.encode("utf-8") if override is not None else path.read_bytes()
        names = self.adapter.function_parameters(
            path=path,
            project_root=self.project_root,
            source=source,
            func_qpath=params["func_qpath"],
            decl_byte_range=tuple(decl) if decl else None,
        )
        return {"parameters": list(names) if names is not None else None}

    def normalize_blank_gaps(self, params: dict) -> dict:
        """PEP 8 blank-run normalization (writes/_blank_lines) on raw source."""
        from wyolet.symbol.writes._blank_lines import normalize_blank_gaps

        source = params["source"].encode("utf-8")
        return {"source": normalize_blank_gaps(source).decode("utf-8", errors="replace")}

    def first_decorator_line(self, params: dict) -> dict:
        """Topmost decorator line of the def/class at `def_line` (insert-symbol).

        Port of writes/insert_symbol.py `_first_decorator_line`: walk the AST
        for the FunctionDef/AsyncFunctionDef/ClassDef whose lineno == def_line
        and return min(decorator linenos). Falls back to def_line itself when
        there are no decorators, no matching node, or the source can't parse
        (an `@` inside a string or a decorator argument must not fool a textual
        scan, hence the AST walk). Read failures bubble to the JSON-RPC error
        path.
        """
        source = Path(params["path"]).read_bytes()
        def_line = int(params["def_line"])
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {"line": def_line}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.lineno == def_line and node.decorator_list:
                    return {"line": min(d.lineno for d in node.decorator_list)}
        return {"line": def_line}

    def project_dependencies(self, params: dict) -> dict:
        """Declared dependencies of the project, normalized — the DEPS
        pipeline that selects which per-package specs apply."""
        from wyolet.symbol.adapters import python_deps  # noqa: F401 — registers the DEPS hooks
        from wyolet.symbol.shared.deps import detect_deps

        root = params.get("project_root") or self.project_root
        return {"dependencies": detect_deps(Path(root)) if root else []}

    def scan_content(self, params: dict) -> dict:
        """Scan in-memory content (replace-symbol validation path)."""
        source = params["source"].encode("utf-8")
        scan = self.adapter.scan_file(
            Path(params.get("path", "<content>")), source,
            module_prefix=params.get("module_prefix", ""),
        )
        return asdict(scan)


def main() -> None:
    stdin = sys.stdin
    stdout = sys.stdout
    daemon = _Daemon(stdin, stdout)
    while True:
        line = stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "shutdown":
            return
        handler = getattr(daemon, method, None) if method and not method.startswith("_") else None
        if handler is None:
            resp: dict = {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"},
            }
        else:
            try:
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": handler(msg.get("params") or {})}
            except Exception as e:  # noqa: BLE001 — daemon must never die on one request
                resp = {
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"},
                }
        if msg_id is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
