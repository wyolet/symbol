"""`ca patch` — CLI wrapper + rendering.

Calls the engine in `writes/patch.py` and renders the result. Rendering
has three modes:
- rich (TTY default): Rich panels/tables with the diff.
- agent (--agent or CA_AGENT=1): plain text, hint-dense.
- json (--format json): structured data, bare facts.
"""

import json as _json
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from ca_tools.caches import build_read_cache
from ca_tools.writes.patch import (
    PatchPreflight,
    PatchRequest,
    PatchResult,
    apply_patch,
    preflight_patch,
    validate_args,
)

console = Console()


def patch_cmd(
    *,
    file: str,
    range: str,
    content: str | None,
    project_root: str = ".",
    force: bool = False,
    dry_run: bool = False,
    context: int = 5,
    agent: bool = False,
    format: str = "rich",
) -> None:
    """Top-level `ca patch` entry. Dispatches, applies, renders, sets exit code."""
    project = Path(project_root).resolve()

    # Stage 1: validate args.
    req = validate_args(
        file=file,
        raw_range=range,
        content=content,
        project_root=project,
        force=force,
    )
    if isinstance(req, PatchPreflight):
        # Validation error.
        _render_preflight(req, format=format, agent=agent)
        sys.exit(1)
    assert isinstance(req, PatchRequest)

    # Stage 1: preflight (cache check).
    cache = build_read_cache()
    pre = preflight_patch(req, cache)

    if pre.status == "needs_read_confirmation":
        _render_preflight(pre, format=format, agent=agent)
        sys.exit(2)

    if pre.status == "error":
        _render_preflight(pre, format=format, agent=agent)
        sys.exit(1)

    # Stage 2: apply.
    result = apply_patch(req, cache=cache, dry_run=dry_run, diff_context=context)
    _render_result(result, format=format, agent=agent)

    if result.status == "error":
        sys.exit(1)


# ---------------------------------------------------------- rendering: preflight


def _render_preflight(pre: PatchPreflight, *, format: str, agent: bool) -> None:
    if format == "json":
        print(_json.dumps(_preflight_to_dict(pre), indent=2))
        return

    if pre.status == "needs_read_confirmation":
        if agent:
            _render_needs_confirmation_agent(pre)
        else:
            _render_needs_confirmation_rich(pre)
        return

    if pre.status == "error":
        _render_error(pre.error_code, pre.message, agent=agent)
        return


def _render_needs_confirmation_agent(pre: PatchPreflight) -> None:
    req = pre.request
    assert req is not None
    current = _read_current_range(req)
    print("status: needs_read_confirmation")
    print(f"file: {req.file_rel}")
    print(f"range: lines {req.line_range[0]}-{req.line_range[1]} (bytes {req.byte_range[0]}-{req.byte_range[1]})")
    print(f"reason: no cached read covers this range.")
    print()
    print("--- CURRENT CONTENT ---")
    print(current, end="" if current.endswith("\n") else "\n")
    print("--- END ---")
    print()
    print("Next: read the range, then retry with --force if the patch is still correct.")


def _render_needs_confirmation_rich(pre: PatchPreflight) -> None:
    req = pre.request
    assert req is not None
    current = _read_current_range(req)

    console.print(f"[yellow]needs_read_confirmation[/yellow]  [dim]{req.file_rel}:{req.line_range[0]}-{req.line_range[1]}[/dim]")
    console.print("[dim]The agent hasn't read this range in this session.[/dim]\n")
    console.print(
        Panel(
            Syntax(current, "python", line_numbers=True, start_line=req.line_range[0]),
            title="current content",
            border_style="dim",
        )
    )
    console.print(
        "\n[dim]After reading, re-run with [bold]--force[/bold] "
        "to bypass the cache check.[/dim]"
    )


# ---------------------------------------------------------- rendering: result


def _render_result(result: PatchResult, *, format: str, agent: bool) -> None:
    if format == "json":
        print(_json.dumps(_result_to_dict(result), indent=2))
        return

    if result.status == "error":
        _render_error(result.error_code, result.message, agent=agent)
        return

    if agent:
        _render_result_agent(result)
    else:
        _render_result_rich(result)


def _render_result_agent(result: PatchResult) -> None:
    verb = "would apply" if result.status == "dry_run" else "applied"
    print(f"status: {result.status}")
    print(f"file: {result.file_rel}")
    print(f"{verb}: -{result.lines_removed} +{result.lines_added} lines")
    if result.status == "applied":
        print("undo: git checkout -- <file>  # revert uncommitted change")
    print()
    if result.diff:
        print("--- DIFF ---")
        print(result.diff, end="" if result.diff.endswith("\n") else "\n")
        print("--- END ---")


def _render_result_rich(result: PatchResult) -> None:
    color = "green" if result.status == "applied" else "yellow"
    title = "applied" if result.status == "applied" else "dry run"
    summary = (
        f"[{color}]{title}[/{color}]  "
        f"[dim]{result.file_rel}[/dim]  "
        f"[dim]-{result.lines_removed} +{result.lines_added} lines[/dim]"
    )
    console.print(summary)
    if result.diff:
        console.print(
            Panel(
                Syntax(result.diff, "diff", line_numbers=False),
                title="diff",
                border_style="dim",
            )
        )


# ---------------------------------------------------------- rendering: errors


def _render_error(code: str | None, message: str | None, *, agent: bool) -> None:
    if agent:
        print(f"status: error")
        print(f"error_code: {code}")
        print(f"message: {message}")
        return
    console.print(f"[red]error[/red]  [bold]{code}[/bold]")
    if message:
        console.print(f"  {message}")


# ---------------------------------------------------------- serializers


def _preflight_to_dict(pre: PatchPreflight) -> dict:
    out: dict = {"status": pre.status}
    if pre.request is not None:
        out["file"] = pre.request.file_rel
        out["line_range"] = list(pre.request.line_range)
        out["byte_range"] = list(pre.request.byte_range)
    if pre.status == "needs_read_confirmation" and pre.request is not None:
        out["current_content"] = _read_current_range(pre.request)
    if pre.error_code:
        out["error_code"] = pre.error_code
        out["message"] = pre.message
    return out


def _result_to_dict(result: PatchResult) -> dict:
    d = asdict(result)
    # JSON naturally serializes tuples as lists; fine.
    # Drop Nones for cleaner output.
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------- helpers


def _read_current_range(req: PatchRequest) -> str:
    """Read and decode the current content at the request's byte range."""
    try:
        data = req.file_abs.read_bytes()
    except OSError:
        return ""
    return data[req.byte_range[0] : req.byte_range[1]].decode("utf-8", errors="replace")
