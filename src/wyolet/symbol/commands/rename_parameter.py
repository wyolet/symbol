"""`symbol rename-parameter` — CLI + rendering.

Thin Typer view mirroring commands/rename_symbol.py: resolve the request,
apply through the neutral engine, render the shared RenameSymbolResult.
"""

import json as _json
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console

from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.rename_parameter import (
    RenameParameterRequest,
    apply_rename_parameter,
    resolve_rename_parameter,
)
from wyolet.symbol.writes.rename_symbol import RenameSymbolResult

console = Console()


def rename_parameter_cmd(
    *,
    func_qpath: str,
    param: str,
    new_name: str,
    project_root: str = ".",
    dry_run: bool = False,
    agent: bool = False,
    format: str = "rich",
) -> None:
    project = Path(project_root).resolve()
    index, _ = get_or_build_index(project)

    resolved = resolve_rename_parameter(index, func_qpath, param, new_name, project)
    if isinstance(resolved, RenameSymbolResult):
        _render(resolved, param=param, format=format, agent=agent)
        sys.exit(1)

    assert isinstance(resolved, RenameParameterRequest)
    result = apply_rename_parameter(
        resolved, project_root=project, dry_run=dry_run, _index=index,
    )
    _render(result, param=param, new_name=new_name, format=format, agent=agent)
    if result.status == "error":
        sys.exit(1)


def _render(
    result: RenameSymbolResult, *, param: str, new_name: str, format: str, agent: bool,
) -> None:
    if format == "json":
        d = asdict(result)
        d["parameter"] = param
        d["new_parameter"] = new_name
        print(_json.dumps({k: v for k, v in d.items() if v not in (None, "", (), 0)}, indent=2))
        return

    if result.status == "error":
        if agent:
            print("status: error")
            print(f"error_code: {result.error_code}")
            print(f"message: {result.message}")
            for c in result.candidates:
                print(f"  candidate: {c}")
        else:
            console.print(f"[red]error[/red]  [bold]{result.error_code}[/bold]")
            if result.message:
                console.print(f"  {result.message}")
            for c in result.candidates:
                console.print(f"  [dim]candidate:[/dim] {c}")
        return

    verb = {
        "applied": "renamed", "dry_run": "would rename", "needs_review": "needs review",
    }.get(result.status, result.status)
    if agent:
        print(f"status: {result.status}")
        print(f"{verb}: {param} → {new_name}  in {result.qualified_path}")
        print(f"files_changed: {result.files_changed}")
        print(f"refs_updated: {result.refs_updated}")
        for f in result.per_file:
            print(f"  {f.file}  ({f.refs_updated} refs)")
    else:
        color = {"applied": "green", "dry_run": "yellow"}.get(result.status, "magenta")
        console.print(
            f"[{color}]{verb}[/{color}]  parameter [bold]{param}[/bold] → "
            f"[bold]{new_name}[/bold] of [bold]{result.qualified_path}[/bold]  "
            f"[dim]{result.files_changed} files, {result.refs_updated} refs[/dim]"
        )
        for f in result.per_file:
            console.print(f"  [dim]{f.file}[/dim]  {f.refs_updated} refs")

    _render_sites(result, agent=agent)

    if result.status == "applied":
        if agent:
            print("\nundo: symbol undo")
        else:
            console.print("\n[dim]Undo:[/dim] [bold]symbol undo[/bold]")


def _render_sites(result: RenameSymbolResult, *, agent: bool) -> None:
    if result.skipped_mismatch:
        msg = f"skipped {len(result.skipped_mismatch)} kwarg site(s) — callee resolved to a different function:"
        if agent:
            print(f"\n{msg}")
            for s in result.skipped_mismatch:
                print(f"  {s.file}:{s.line}:{s.col + 1}  `{s.receiver_source}(...)`  → {s.resolved_to_qpath}")
        else:
            console.print(f"\n[dim]{msg}[/dim]")
            for s in result.skipped_mismatch:
                console.print(
                    f"  [dim]{s.file}:{s.line}:{s.col + 1}[/dim]  "
                    f"[yellow]{s.receiver_source}(...)[/yellow] → [bold]{s.resolved_to_qpath}[/bold]"
                )
    if result.unresolved:
        msg = f"unresolved {len(result.unresolved)} kwarg site(s) — callee could not be resolved statically:"
        if agent:
            print(f"\n{msg}")
            for u in result.unresolved:
                print(f"  {u.file}:{u.line}:{u.col + 1}  `{u.receiver_source}(...)`  ({u.why})")
        else:
            console.print(f"\n[bold magenta]needs review[/bold magenta] — {msg}")
            for u in result.unresolved:
                console.print(
                    f"  [dim]{u.file}:{u.line}:{u.col + 1}[/dim]  "
                    f"[yellow]{u.receiver_source}(...)[/yellow]  [dim]({u.why})[/dim]"
                )
