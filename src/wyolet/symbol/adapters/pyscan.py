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
  shutdown         (notification) -> exits

Run: python -m wyolet.symbol.adapters.pyscan
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from wyolet.symbol.adapters.python_ast import PythonAstAdapter

_PROTOCOL_VERSION = "1"


class _Daemon:
    def __init__(self) -> None:
        self.adapter = PythonAstAdapter()
        self.project_root: Path | None = None

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

    def normalize_blank_gaps(self, params: dict) -> dict:
        """PEP 8 blank-run normalization (writes/_blank_lines) on raw source."""
        from wyolet.symbol.writes._blank_lines import normalize_blank_gaps

        source = params["source"].encode("utf-8")
        return {"source": normalize_blank_gaps(source).decode("utf-8", errors="replace")}

    def scan_content(self, params: dict) -> dict:
        """Scan in-memory content (replace-symbol validation path)."""
        source = params["source"].encode("utf-8")
        scan = self.adapter.scan_file(
            Path(params.get("path", "<content>")), source,
            module_prefix=params.get("module_prefix", ""),
        )
        return asdict(scan)


def main() -> None:
    daemon = _Daemon()
    stdin = sys.stdin
    stdout = sys.stdout
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
