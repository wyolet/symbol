"""`symbol insert-symbol` — CLI + rendering."""

import json as _json
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from wyolet.symbol.caches import build_read_cache
from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.insert_symbol import (
    InsertSymbolRequest,
    InsertSymbolResult,
    apply_insert_symbol,
    resolve_insert_symbol,
)

console = Console()


def insert_symbol_cmd(
    *,
    anchor: str,
    position: str,
    content: str | None,
    project_root: str = ".",
    reindent: bool = True,
    dry_run: bool = False,
    context: int = 5,
    agent: bool = False,
    format: str = "rich",
) -> None:
    project = Path(project_root).resolve()
    index, _ = get_or_build_index(project)

    resolved = resolve_insert_symbol(
        index, anchor, position, content or "", project, reindent=reindent,
    )
    if isinstance(resolved, InsertSymbolResult):
        _render(resolved, format=format, agent=agent)
        sys.exit(1)

    assert isinstance(resolved, InsertSymbolRequest)
    cache = build_read_cache()
    result = apply_insert_symbol(
        resolved, cache=cache, dry_run=dry_run, diff_context=context,
    )
    _render(result, format=format, agent=agent)
    if result.status == "error":
        sys.exit(1)


def _render(result: InsertSymbolResult, *, format: str, agent: bool) -> None:
    if format == "json":
        print(_json.dumps({k: v for k, v in asdict(result).items() if v not in (None, "", (), 0)}, indent=2))
        return

    if result.status == "error":
        if agent:
            print(f"status: error\nerror_code: {result.error_code}\nmessage: {result.message}")
            for c in result.candidates:
                print(f"  candidate: {c}")
            return
        console.print(f"[red]error[/red]  [bold]{result.error_code}[/bold]")
        if result.message:
            console.print(f"  {result.message}")
        for c in result.candidates:
            console.print(f"  [dim]candidate:[/dim] {c}")
        return

    if agent:
        verb = "would insert" if result.status == "dry_run" else "inserted"
        print(f"status: {result.status}")
        print(f"{verb}: {result.position} {result.anchor_path}  ({result.anchor_kind})")
        print(f"file: {result.file_rel}:{result.insert_line}")
        print(f"lines_added: {result.lines_added}")
        if result.diff:
            print()
            print("--- DIFF ---")
            print(result.diff, end="" if result.diff.endswith("\n") else "\n")
            print("--- END ---")
        return

    color = "green" if result.status == "applied" else "yellow"
    title = "inserted" if result.status == "applied" else "dry run"
    console.print(
        f"[{color}]{title}[/{color}]  "
        f"[dim]{result.position}[/dim]  "
        f"[bold]{result.anchor_path}[/bold]  "
        f"[dim]({result.anchor_kind})  "
        f"{result.file_rel}:{result.insert_line}  "
        f"+{result.lines_added} lines[/dim]"
    )
    if result.diff:
        console.print(
            Panel(
                Syntax(result.diff, "diff", line_numbers=False),
                title="diff",
                border_style="dim",
            )
        )
