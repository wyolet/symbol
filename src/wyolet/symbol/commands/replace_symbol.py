"""`symbol replace-symbol` — CLI + rendering."""

import json as _json
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.replace_symbol import (
    ReplaceSymbolRequest,
    ReplaceSymbolResult,
    apply_replace_symbol,
    resolve_replace_symbol,
)

console = Console()


def replace_symbol_cmd(
    *,
    qualified_path: str,
    content: str,
    project_root: str = ".",
    dry_run: bool = False,
    agent: bool = False,
    format: str = "rich",
) -> None:
    project = Path(project_root).resolve()
    index, _ = get_or_build_index(project)

    resolved = resolve_replace_symbol(index, qualified_path, content, project)
    if isinstance(resolved, ReplaceSymbolResult):
        _render(resolved, format=format, agent=agent)
        sys.exit(1)

    assert isinstance(resolved, ReplaceSymbolRequest)
    result = apply_replace_symbol(
        resolved,
        project_root=project,
        dry_run=dry_run,
    )
    _render(result, format=format, agent=agent)
    if result.status == "error":
        sys.exit(1)


def _render(result: ReplaceSymbolResult, *, format: str, agent: bool) -> None:
    if format == "json":
        print(_json.dumps(_to_dict(result), indent=2))
        return

    if result.status == "error":
        _render_error(result, agent=agent)
        return

    if agent:
        _render_agent(result)
    else:
        _render_rich(result)


def _render_agent(result: ReplaceSymbolResult) -> None:
    verb = "would replace" if result.status == "dry_run" else "replaced"
    print(f"status: {result.status}")
    if result.name_changed:
        print(f"{verb}: {result.qualified_path} → {result.new_qualified_path}")
        print(f"name_changed: true")
    else:
        print(f"{verb}: {result.qualified_path}")
        print(f"name_changed: false")
    print(f"kind: {result.kind}")
    if result.new_signature:
        print(f"signature: {result.new_signature}")
    print(f"file: {result.declaring_file}")
    print(f"files_changed: {result.files_changed}")
    if result.name_changed:
        print(f"refs_updated: {result.refs_updated}")
    if result.per_file:
        print()
        for f in result.per_file:
            print(f"  {f.file}  ({f.refs_updated} change{'s' if f.refs_updated != 1 else ''})")
    if result.status == "applied":
        print()
        print("undo: git reset --hard HEAD~1")


def _render_rich(result: ReplaceSymbolResult) -> None:
    color = "green" if result.status == "applied" else "yellow"
    title = "replaced" if result.status == "applied" else "dry run"
    arrow = (
        f"[bold]{result.qualified_path}[/bold] → [bold]{result.new_qualified_path}[/bold]"
        if result.name_changed
        else f"[bold]{result.qualified_path}[/bold]"
    )
    console.print(
        f"[{color}]{title}[/{color}]  {arrow}  "
        f"[dim]({result.kind})  "
        f"{result.files_changed} file(s), {result.refs_updated} change(s)[/dim]"
    )
    if result.new_signature:
        console.print(f"  [dim]new signature:[/dim] [cyan]{result.new_signature}[/cyan]")
    if result.per_file:
        t = Table(show_header=True, header_style="bold")
        t.add_column("file")
        t.add_column("changes", justify="right")
        for f in result.per_file:
            t.add_row(f.file, str(f.refs_updated))
        console.print(t)
    if result.status == "applied":
        console.print("\n[dim]Undo:[/dim] [bold]git reset --hard HEAD~1[/bold]")


def _render_error(result: ReplaceSymbolResult, *, agent: bool) -> None:
    if agent:
        print(f"status: error")
        print(f"error_code: {result.error_code}")
        print(f"message: {result.message}")
        for c in result.candidates:
            print(f"  candidate: {c}")
        return
    console.print(f"[red]error[/red]  [bold]{result.error_code}[/bold]")
    if result.message:
        console.print(f"  {result.message}")
    for c in result.candidates:
        console.print(f"  [dim]candidate:[/dim] {c}")


def _to_dict(result: ReplaceSymbolResult) -> dict:
    d = asdict(result)
    return {k: v for k, v in d.items() if v not in (None, "", (), 0, False)}
