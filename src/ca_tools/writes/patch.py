"""`ca patch` — byte-range edit primitive.

Stage 1 (this module): argument validation and cache preflight. No file
writes yet. Returns a structured preflight result that later stages will
consume to either apply, stage, or report a conflict.

Preflight branches:
- `ok`: cache confirms agent saw the target range (or --force was passed).
  The caller may proceed to stage the edit.
- `needs_read_confirmation`: no cache coverage and --force was not set.
  Caller should return the current content to the agent with a confirm token.
- `error`: input is invalid — bad range, missing file, etc. No op proceeds.
"""

import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ca_tools.protocols import CachedRead, ReadCache


_RANGE_RE = re.compile(r"^(?P<start>\d+)-(?P<end>\d+)$")


@dataclass(frozen=True)
class PatchRequest:
    """Validated patch input, ready for downstream stages.

    `file_abs` is the resolved absolute path. `file_rel` is the path as it
    appears in cache keys and responses (relative to the project root).
    Content is bytes — encoding is caller's responsibility.
    """

    file_abs: Path
    file_rel: str
    line_range: tuple[int, int]
    byte_range: tuple[int, int]
    content: bytes
    force: bool


@dataclass(frozen=True)
class PatchPreflight:
    """Result of preflight check: argument validation + cache lookup."""

    status: Literal["ok", "needs_read_confirmation", "error"]
    request: PatchRequest | None = None
    # Populated when status == ok:
    cache_entry: CachedRead | None = None
    # Populated when status == needs_read_confirmation:
    current_byte_range: tuple[int, int] | None = None
    # Populated when status == error:
    error_code: str | None = None
    message: str | None = None


class InvalidRange(ValueError):
    """Raised by parse_line_range on bad input."""


# ---------------------------------------------------------- argument parsing


def parse_line_range(raw: str) -> tuple[int, int]:
    """Parse a `"A-B"` string into (A, B). Both ends inclusive, 1-indexed.

    Raises InvalidRange on bad input. Allows A == B (single line or zero-
    width insert). A must be <= B.
    """
    m = _RANGE_RE.match(raw.strip())
    if m is None:
        raise InvalidRange(f"range must be 'A-B' with non-negative integers, got {raw!r}")
    start = int(m["start"])
    end = int(m["end"])
    if start < 1:
        raise InvalidRange(f"range start must be >= 1, got {start}")
    if end < start:
        raise InvalidRange(f"range end ({end}) must be >= start ({start})")
    return (start, end)


# ---------------------------------------------------------- byte range from lines


def line_range_to_byte_range(
    source: bytes, line_range: tuple[int, int]
) -> tuple[int, int]:
    """Convert a line range (inclusive, 1-indexed) to a byte range (end exclusive).

    Line N's bytes are from the byte just after the (N-1)th newline through
    and including the Nth newline. End of range includes the trailing
    newline of the last line, if present.

    For a zero-width insert (signaled by end == start - 1), returns a
    zero-width byte range at the start of `start`. Not reachable from
    parse_line_range (which enforces end >= start) but may come from
    internal callers.
    """
    start_line, end_line = line_range
    start = _line_start_byte(source, start_line)
    end = _line_end_byte(source, end_line)
    return (start, end)


def _line_start_byte(data: bytes, line: int) -> int:
    if line <= 1:
        return 0
    seen = 1
    for i, b in enumerate(data):
        if b == 0x0A:
            seen += 1
            if seen == line:
                return i + 1
    return len(data)


def _line_end_byte(data: bytes, line: int) -> int:
    seen = 0
    for i, b in enumerate(data):
        if b == 0x0A:
            seen += 1
            if seen == line:
                return i + 1
    return len(data)


# ---------------------------------------------------------- validation


def validate_args(
    *,
    file: str,
    raw_range: str,
    content: str | bytes | None,
    project_root: Path,
    force: bool = False,
) -> PatchRequest | PatchPreflight:
    """Validate CLI args into a PatchRequest, or return an error PatchPreflight.

    Returns a PatchRequest when everything checks out; otherwise a
    PatchPreflight with status='error' and a specific error_code.
    """
    # Resolve file path.
    file_abs = Path(file).resolve()
    if not file_abs.exists():
        return _error("file_not_found", f"no such file: {file}")
    if not file_abs.is_file():
        return _error("file_not_found", f"not a regular file: {file}")

    # Read to check size + get bytes for byte-range conversion.
    try:
        source = file_abs.read_bytes()
    except OSError as e:
        return _error("file_not_found", f"cannot read {file}: {e}")

    # Binary-file rejection: patch is text-oriented for v1.
    if _looks_binary(source):
        return _error("binary_file", f"{file} appears to be binary")

    # Range parsing.
    try:
        line_range = parse_line_range(raw_range)
    except InvalidRange as e:
        return _error("invalid_argument", str(e))

    # Range bounds.
    total_lines = _count_lines(source)
    if line_range[1] > total_lines:
        return _error(
            "range_out_of_bounds",
            f"range end {line_range[1]} exceeds file length ({total_lines} lines)",
        )

    # Content encoding.
    if content is None:
        payload = b""
    elif isinstance(content, bytes):
        payload = content
    else:
        payload = content.encode("utf-8")

    # Compute the byte range we'll operate on (from current file bytes).
    byte_range = line_range_to_byte_range(source, line_range)

    # Compute project-relative path for cache keys.
    try:
        file_rel = str(file_abs.relative_to(project_root.resolve()))
    except ValueError:
        # File is outside project root; use absolute as the rel key.
        file_rel = str(file_abs)

    return PatchRequest(
        file_abs=file_abs,
        file_rel=file_rel,
        line_range=line_range,
        byte_range=byte_range,
        content=payload,
        force=force,
    )


def _looks_binary(data: bytes) -> bool:
    """Heuristic: treat as binary if we see a null byte in the first 8 KB."""
    return b"\x00" in data[:8192]


def _count_lines(data: bytes) -> int:
    if not data:
        return 0
    count = data.count(b"\n")
    # Trailing content without a newline still counts as a line.
    if not data.endswith(b"\n"):
        count += 1
    return count


def _error(code: str, message: str) -> PatchPreflight:
    return PatchPreflight(status="error", error_code=code, message=message)


# ---------------------------------------------------------- preflight


def preflight_patch(
    request: PatchRequest,
    cache: ReadCache,
) -> PatchPreflight:
    """Check whether the agent has seen the target range.

    Three outcomes:
    - `--force`: bypass cache check, return ok.
    - Covering cache entry found with matching mtime: return ok.
    - No coverage (or mtime mismatch for stage 1): return
      needs_read_confirmation.

    Stage 1 treats mtime mismatch the same as no coverage. Full conflict
    handling (with re-hashing of the covering range) lands in stage 2.
    """
    if request.force:
        return PatchPreflight(status="ok", request=request)

    covering = cache.find_covering(Path(request.file_rel), request.byte_range)

    if covering is not None:
        try:
            current_mtime = os.stat(request.file_abs).st_mtime
        except OSError:
            current_mtime = None
        if current_mtime is not None and covering.served_mtime == current_mtime:
            return PatchPreflight(status="ok", request=request, cache_entry=covering)

    return PatchPreflight(
        status="needs_read_confirmation",
        request=request,
        current_byte_range=request.byte_range,
    )


# ---------------------------------------------------------- apply (stage 2)


@dataclass(frozen=True)
class PatchResult:
    """Outcome of applying a patch. Pure data — rendering happens in commands/."""

    status: Literal["applied", "dry_run", "error"]
    file_rel: str
    # Byte range replaced in the original file.
    before_range: tuple[int, int] = (0, 0)
    # Byte range occupied by the new content in the resulting file.
    after_range: tuple[int, int] = (0, 0)
    lines_removed: int = 0
    lines_added: int = 0
    diff: str = ""
    # Populated when status == error:
    error_code: str | None = None
    message: str | None = None


def apply_patch(
    request: PatchRequest,
    *,
    cache: ReadCache,
    dry_run: bool = False,
    diff_context: int = 5,
) -> PatchResult:
    """Splice new content into the file at the request's byte range.

    Preflight is assumed to have passed. We still re-read the file and
    verify bounds, because the file could have changed between preflight
    and apply — if so, we report a conflict rather than write corrupt
    content.

    On dry_run: computes the diff and returns `status="dry_run"` without
    touching the file or invalidating the cache.
    """
    try:
        source = request.file_abs.read_bytes()
    except OSError as e:
        return _apply_error("file_not_found", f"cannot read {request.file_rel}: {e}", request)

    start, end = request.byte_range
    if end > len(source) or start > len(source):
        return _apply_error(
            "conflict",
            f"file shrank: byte range {start}-{end} exceeds current size ({len(source)} bytes)",
            request,
        )

    new_source = source[:start] + request.content + source[end:]
    old_slice = source[start:end]

    diff = _unified_diff(request.file_rel, source, new_source, context=diff_context)
    lines_removed = _count_lines(old_slice)
    lines_added = _count_lines(request.content)
    after_range = (start, start + len(request.content))

    if dry_run:
        return PatchResult(
            status="dry_run",
            file_rel=request.file_rel,
            before_range=(start, end),
            after_range=after_range,
            lines_removed=lines_removed,
            lines_added=lines_added,
            diff=diff,
        )

    try:
        _atomic_write(request.file_abs, new_source)
    except PermissionError as e:
        return _apply_error("permission_denied", str(e), request)
    except OSError as e:
        return _apply_error("file_not_found", f"write failed: {e}", request)

    cache.invalidate(Path(request.file_rel))

    return PatchResult(
        status="applied",
        file_rel=request.file_rel,
        before_range=(start, end),
        after_range=after_range,
        lines_removed=lines_removed,
        lines_added=lines_added,
        diff=diff,
    )


def _apply_error(code: str, message: str, request: PatchRequest) -> PatchResult:
    return PatchResult(
        status="error",
        file_rel=request.file_rel,
        error_code=code,
        message=message,
    )


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via tmp file + fsync + rename. Preserves original mode if present."""
    original_mode = None
    try:
        original_mode = os.stat(path).st_mode
    except OSError:
        pass

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ca-tools.tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if original_mode is not None:
            os.chmod(tmp, original_mode & 0o7777)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _unified_diff(
    file_rel: str,
    old_source: bytes,
    new_source: bytes,
    *,
    context: int = 5,
) -> str:
    """Full-file unified diff. Hunk headers carry absolute file line numbers
    and each diff line is prefixed with its absolute line number so agents
    can reason about lines without confusion from relative positions.

    Format per line: `{sign} {line}  {content}`. Signs are ` ` (context),
    `-` (removed, old-file line number), `+` (added, new-file line number).
    """
    old_text = old_source.decode("utf-8", errors="replace").splitlines(keepends=True)
    new_text = new_source.decode("utf-8", errors="replace").splitlines(keepends=True)
    raw = "".join(
        difflib.unified_diff(
            old_text,
            new_text,
            fromfile=f"{file_rel} (before)",
            tofile=f"{file_rel} (after)",
            n=context,
        )
    )
    return _number_diff_lines(raw)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _number_diff_lines(diff: str) -> str:
    """Prefix each diff line with its absolute file line number.

    Context and `+` lines get the new-file line number.
    `-` lines get the old-file line number.
    Hunk headers and file headers are passed through unchanged.
    """
    out: list[str] = []
    old_line = 0
    new_line = 0
    for line in diff.splitlines(keepends=True):
        if line.startswith("@@"):
            m = _HUNK_RE.match(line.rstrip())
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
            out.append(line)
        elif line.startswith("---") or line.startswith("+++"):
            out.append(line)
        elif line.startswith(" "):
            out.append(f" {new_line:>4} {line[1:]}")
            old_line += 1
            new_line += 1
        elif line.startswith("-"):
            out.append(f"-{old_line:>4} {line[1:]}")
            old_line += 1
        elif line.startswith("+"):
            out.append(f"+{new_line:>4} {line[1:]}")
            new_line += 1
        else:
            out.append(line)
    return "".join(out)
