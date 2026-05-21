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

import atexit
import json
import os
import platform
import subprocess
import threading
from pathlib import Path

from wyolet.symbol.protocols.types import (
    FileScan,
    ParseResult,
    RawSymbol,
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
        self._atexit_registered = False
        # One in-flight request at a time. v1 doesn't need pipelined RPC,
        # and the daemon's reader is line-oriented so interleaving would
        # corrupt the framing anyway.
        self._lock = threading.Lock()
        # Per-adapter caches for module_prefix resolution. Cleared only
        # on adapter destruction; go.mod doesn't change mid-session in any
        # realistic workflow.
        self._gomod_cache: dict[Path, tuple[Path | None, str | None]] = {}
        self._modpath_cache: dict[Path, str] = {}

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
        # Register once per adapter instance — atexit handlers stack and
        # we only want one per spawned daemon. Re-registration on respawn
        # is fine because _stop() is idempotent.
        if not self._atexit_registered:
            atexit.register(self._stop)
            self._atexit_registered = True
        self._call_raw("initialize", {"protocol_version": _PROTOCOL_VERSION})

    def _stop(self) -> None:
        """Send shutdown and reap the daemon. Idempotent and safe to call
        from atexit, __del__, or test teardown. No exceptions propagate.
        """
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.poll() is not None:
            return  # already exited
        try:
            # Best-effort graceful shutdown via the protocol's own notification.
            if proc.stdin is not None and not proc.stdin.closed:
                notif = {"jsonrpc": "2.0", "method": "shutdown", "params": {}}
                try:
                    proc.stdin.write(json.dumps(notif) + "\n")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except Exception:
            # atexit handlers must not raise — anything else here is a
            # cleanup error and we're already shutting down anyway.
            pass

    def __del__(self) -> None:
        # Belt-and-braces in case the adapter is GC'd before atexit fires
        # (e.g. between test cases that re-instantiate the registry).
        try:
            self._stop()
        except Exception:
            pass

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

    def symbols(self, path: Path, source: bytes) -> list[RawSymbol]:
        """Top-level symbols declared in ``source``.

        Projection of ``scan_file``: ``ScannedSymbol`` (which carries refs
        from its scope) → ``RawSymbol`` (which doesn't — that's the
        SemanticLanguageAdapter tier). ``signature_line`` is the symbol's
        first line, matching the Python adapter's convention.

        Go requires every file to begin with a ``package`` declaration,
        so a snippet that's "just a function" won't parse on its own.
        When the input lacks a package line we prepend a synthetic one
        and shift byte offsets back so the returned ranges still point
        into the caller-supplied bytes. The Python ``ast`` module accepts
        bare module-level code; we paper over Go's stricter requirement
        here so write engines have a uniform contract across languages.
        """
        text = source.decode("utf-8", errors="replace")
        prelude = ""
        if not _has_package_decl(text):
            prelude = "package _stub\n"
            text = prelude + text

        scan = self.scan_file(path, text.encode("utf-8"))
        out: list[RawSymbol] = []
        shift = len(prelude.encode("utf-8"))
        prelude_lines = prelude.count("\n")
        for s in scan.symbols:
            out.append(_rawsymbol_from_scanned(s, byte_shift=-shift, line_shift=-prelude_lines))
        return out

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

    def module_prefix(self, path: Path, project_root: Path) -> str:
        """Go qualified-path prefix: the package's import path.

        Walks up from the file's directory to find the nearest ``go.mod``,
        reads its ``module`` declaration, then appends the directory path
        relative to that go.mod. For ``github.com/foo/bar/pkg/user/u.go``
        under a go.mod that declares ``module github.com/foo/bar``, the
        result is ``github.com/foo/bar/pkg/user``.

        Falls back to the directory path under project_root when no
        go.mod is found (e.g. one-off scripts, test fixtures). Result
        cached per directory; go.mod parsing is cheap but the walk isn't
        free at scale.
        """
        directory = path.parent.resolve()
        cached = self._modpath_cache.get(directory)
        if cached is not None:
            return cached

        module_root, module_name = self._find_go_module(directory)
        if module_root is None or module_name is None:
            # No go.mod ancestor — fall back to directory layout from
            # project root. Better than nothing for one-off files.
            try:
                rel_dir = path.parent.resolve().relative_to(project_root.resolve())
                prefix = "/".join(p for p in str(rel_dir).split("/") if p and p != ".")
            except ValueError:
                prefix = ""
        else:
            try:
                rel_dir = directory.relative_to(module_root)
            except ValueError:
                rel_dir = Path()
            rel_str = "/".join(p for p in str(rel_dir).split("/") if p and p != ".")
            prefix = module_name if not rel_str else f"{module_name}/{rel_str}"

        self._modpath_cache[directory] = prefix
        return prefix

    def _find_go_module(self, start: Path) -> tuple[Path | None, str | None]:
        """Walk up from ``start`` to find the nearest go.mod and parse out
        its ``module`` declaration. Returns ``(module_root, module_name)``
        or ``(None, None)`` if no go.mod exists in any ancestor.
        """
        current: Path | None = start
        while current is not None:
            cached = self._gomod_cache.get(current)
            if cached is not None:
                return cached
            gomod = current / "go.mod"
            if gomod.is_file():
                name = self._parse_go_module_name(gomod)
                result = (current, name) if name else (None, None)
                self._gomod_cache[current] = result
                return result
            parent = current.parent
            if parent == current:
                break
            current = parent
        return (None, None)

    @staticmethod
    def _parse_go_module_name(gomod: Path) -> str | None:
        """Extract the module path from a ``go.mod`` file.

        Reads only the leading lines — ``module`` is required to be the
        first directive per the spec. Comments and ``//`` are skipped.
        """
        try:
            with gomod.open(encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("//"):
                        continue
                    if line.startswith("module"):
                        # ``module github.com/foo/bar`` or ``module "github.com/foo/bar"``
                        rest = line[len("module"):].strip()
                        if rest.startswith('"') and rest.endswith('"'):
                            rest = rest[1:-1]
                        return rest.split()[0] if rest else None
                    # First non-comment non-blank line that isn't ``module`` —
                    # malformed go.mod, give up.
                    return None
        except OSError:
            return None
        return None

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
        refs=tuple(_ref_from_dict(r) for r in (d.get("refs") or [])),
        children=tuple(_symbol_from_dict(c) for c in (d.get("children") or [])),
    )


def _rawsymbol_from_scanned(
    s: ScannedSymbol, *, byte_shift: int = 0, line_shift: int = 0
) -> RawSymbol:
    """ScannedSymbol → RawSymbol projection.

    Drops refs (RawSymbol carries no refs — that's the
    SemanticLanguageAdapter tier). ``signature_line`` is the symbol's
    first line. ``byte_shift`` / ``line_shift`` translate ranges back to
    caller coordinates when the adapter parsed a wrapped buffer (see
    ``GoAstAdapter.symbols``).
    """
    br = (s.byte_range[0] + byte_shift, s.byte_range[1] + byte_shift)
    lr = (s.line_range[0] + line_shift, s.line_range[1] + line_shift)
    return RawSymbol(
        kind=s.kind,
        name=s.name,
        qualified_path=s.qualified_path,
        byte_range=br,
        line_range=lr,
        signature_line=lr[0],
        children=tuple(
            _rawsymbol_from_scanned(c, byte_shift=byte_shift, line_shift=line_shift)
            for c in s.children
        ),
    )


def _has_package_decl(text: str) -> bool:
    """True if ``text`` already starts with a Go ``package`` declaration
    (ignoring leading blank lines, comments, and the build-constraint
    ``//go:build`` directive that comes before package in real files).
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("/*"):
            continue
        return line.startswith("package ") or line == "package"
    return False


def _filescan_from_dict(d: dict) -> FileScan:
    return FileScan(
        language=d["language"],
        ok=bool(d["ok"]),
        error=d.get("error"),
        imports=tuple(_import_from_dict(i) for i in (d.get("imports") or [])),
        symbols=tuple(_symbol_from_dict(s) for s in (d.get("symbols") or [])),
    )
