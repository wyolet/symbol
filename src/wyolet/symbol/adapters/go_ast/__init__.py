"""Go AST adapter — JSON-RPC client paired with the Go daemon under daemon/.

Tier: AST (mid). Language: Go.

Capabilities: symbol index, signatures, imports (no type resolution — that's
the planned GoLspAdapter tier).

Requires: a built ``go-scan`` binary. In dev builds, run ``go build`` inside
``adapters/go_ast/daemon/``; in wheels, CI cross-compiles per platform and
bundles binaries under ``src/wyolet/symbol/bin/go-scan-<platform>``.

Enable: install Go (>= 1.22) and run the dev build, or install a wheel that
bundles the binary for your platform.

Disabled fallback: none yet (a tier-low GoTreeSitterAdapter is planned).
While disabled, ``.go`` files are skipped during indexing — same as any
unsupported language.
"""

import json
import os
import platform
import subprocess
import threading
from pathlib import Path

from wyolet.symbol.protocols.types import (
    FileScan,
    ParseResult,
    ScannedImport,
    ScannedRef,
    ScannedSymbol,
)

_PROTOCOL_VERSION = "1"

# Where to look for the daemon binary, in order. The dev path lets a
# contributor `go build .` inside daemon/ and have the adapter pick it up
# without further wiring; the bundled path is what wheels ship.
_DEV_BINARY = Path(__file__).parent / "daemon" / "go-scan"


def _bundled_binary() -> Path:
    """Wheel-bundled binary path for the current platform."""
    arch = platform.machine().lower()
    if arch == "x86_64":
        arch = "amd64"
    elif arch in ("aarch64", "arm64"):
        arch = "arm64"
    system = platform.system().lower()
    suffix = ".exe" if system == "windows" else ""
    name = f"go-scan-{system}-{arch}{suffix}"
    return Path(__file__).parent.parent.parent / "bin" / name


class GoAstAdapter:
    """JSON-RPC client wrapping the go-scan daemon."""

    lang = "go"

    def __init__(self) -> None:
        self._binary_path = self._find_binary()
        self._proc: subprocess.Popen | None = None
        self._request_id = 0
        # One in-flight request at a time. v1 doesn't need pipelined RPC,
        # and the daemon's reader is line-oriented so interleaving would
        # corrupt the framing anyway.
        self._lock = threading.Lock()

    @property
    def is_enabled(self) -> bool:
        return self._binary_path is not None

    @staticmethod
    def _find_binary() -> Path | None:
        for candidate in (_DEV_BINARY, _bundled_binary()):
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate
        return None

    # ── lifecycle ────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._binary_path is None:
            raise RuntimeError("go-scan binary not found; adapter is disabled")
        self._proc = subprocess.Popen(
            [str(self._binary_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,  # line-buffered stdout
        )
        self._request_id = 0
        self._call_raw("initialize", {"protocol_version": _PROTOCOL_VERSION})

    def _call_raw(self, method: str, params: dict) -> object:
        """Send one request and read one response. Caller holds ``self._lock``
        for serialization; this helper does the wire work only.
        """
        assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
        self._request_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            stderr = self._proc.stderr.read() if self._proc.stderr else ""
            raise RuntimeError(f"go-scan exited unexpectedly: {stderr.strip()}")
        resp = json.loads(line)
        if "error" in resp and resp["error"]:
            err = resp["error"]
            raise RuntimeError(f"go-scan {method}: [{err.get('code')}] {err.get('message')}")
        return resp.get("result")

    def _call(self, method: str, params: dict) -> object:
        with self._lock:
            self._ensure_started()
            return self._call_raw(method, params)

    def invalidate(self, path: Path) -> None:
        """No-op: the daemon is stateless per-request in v1."""
        return None

    # ── LanguageAdapter surface ──────────────────────────────────

    def scan_file(self, path: Path, source: bytes, *, module_prefix: str = "") -> FileScan:
        result = self._call(
            "scan_file",
            {
                "path": str(path),
                "source": source.decode("utf-8", errors="replace"),
                "module_prefix": module_prefix,
            },
        )
        return _filescan_from_dict(result)  # type: ignore[arg-type]

    def validate_syntax(self, source: bytes) -> ParseResult:
        result = self._call(
            "validate_syntax",
            {"source": source.decode("utf-8", errors="replace")},
        )
        d = result or {}
        return ParseResult(
            ok=bool(d.get("ok")),
            error_line=d.get("error_line"),
            error_message=d.get("error_message"),
        )

    def module_prefix(self, rel_path: str) -> str:
        """Go qualified-path prefix for a repo-relative file path.

        Computed Python-side from the file's directory components. The
        daemon could read ``go.mod`` itself, but that needs filesystem
        access and the dir-path heuristic is correct for any monorepo
        layout where directories mirror import paths (the Go convention).
        """
        parts = [p for p in rel_path.split("/")[:-1] if p]
        return "/".join(parts)

    def signature_from_text(self, text: str) -> str:
        """Go declaration line(s) — up to and including the body-opening ``{``.

        Same shape as the Python adapter's colon-based parser, but the
        body delimiter is ``{`` and Go strings can be backtick-quoted.
        Skips delimiters inside strings, parens, and brackets.
        """
        depth = 0
        in_str = False
        quote = ""
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            if in_str:
                if c == "\\" and quote != "`":
                    i += 2
                    continue
                if c == quote:
                    in_str = False
            else:
                if c in ('"', "'", "`"):
                    in_str = True
                    quote = c
                elif c in "([":
                    depth += 1
                elif c in ")]":
                    depth -= 1
                elif c == "{" and depth == 0:
                    return " ".join(text[: i + 1].split())
            i += 1
        return text.splitlines()[0].strip() if text else ""

    def preview(self, body: str, signature: str, max_lines: int = 3) -> str:
        """First few meaningful body lines after the signature.

        Skips blank lines and ``//`` comments. Preserves indentation. The
        Go equivalent of PythonAstAdapter.preview — different comment
        marker, no docstring concept.
        """
        lines = body.splitlines()
        consumed = 0
        joined = ""
        sig_compact = signature.replace(" ", "") if signature else ""
        for i, line in enumerate(lines):
            joined = (joined + " " + line.strip()).strip()
            if sig_compact and joined.replace(" ", "").endswith(sig_compact):
                consumed = i + 1
                break

        out: list[str] = []
        for line in lines[consumed:]:
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            out.append(line)
            if len(out) >= max_lines:
                break
        return "\n".join(out)


# ── wire → dataclass conversion ─────────────────────────────────


def _ref_from_dict(d: dict) -> ScannedRef:
    return ScannedRef(name=d["name"], kind=d["kind"], line=d["line"])


def _import_from_dict(d: dict) -> ScannedImport:
    return ScannedImport(local=d["local"], source=d["source"], line=d["line"])


def _symbol_from_dict(d: dict) -> ScannedSymbol:
    return ScannedSymbol(
        kind=d["kind"],
        name=d["name"],
        qualified_path=d["qualified_path"],
        byte_range=(d["byte_range"][0], d["byte_range"][1]),
        line_range=(d["line_range"][0], d["line_range"][1]),
        refs=tuple(_ref_from_dict(r) for r in d.get("refs", [])),
        children=tuple(_symbol_from_dict(c) for c in d.get("children", [])),
    )


def _filescan_from_dict(d: dict) -> FileScan:
    return FileScan(
        language=d["language"],
        ok=bool(d["ok"]),
        error=d.get("error"),
        imports=tuple(_import_from_dict(i) for i in d.get("imports", [])),
        symbols=tuple(_symbol_from_dict(s) for s in d.get("symbols", [])),
    )
