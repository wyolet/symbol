"""`symbol delete-symbol` — CLI + rendering."""

import json as _json
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from ca.symbol.caches import build_read_cache
from ca.symbol.shared.symbol_index import get_or_build_index
from ca.symbol.writes.delete_symbol import (
    DeleteSymbolRequest,
    DeleteSymbolResult,
    apply_delete_symbol,
    resolve_delete_symbol,
)

console = Console()


def delete_symbol_cmd(
    *,
    qualified_path: str,
    project_root: str = ".",
    force: bool = False,
    dry_run: bool = False,
    context: int = 5,
    agent: bool = False,
    format: str = "rich",
) -> None:
    project = Path(project_root).resolve()
    index, _source = get_or_build_index(project)

    resolved = resolve_delete_symbol(index, qualified_path, project, force=force)

    if isinstance(resolved, DeleteSymbolResult):
        _render(resolved, format=format, agent=agent)
        sys.exit(1)

    assert isinstance(resolved, DeleteSymbolRequest)
    cache = build_read_cache()
    result = apply_delete_symbol(
        resolved, cache=cache, dry_run=dry_run, diff_context=context
    )

    _render(result, format=format, agent=agent)
    if result.status == "error":
        sys.exit(1)


# ---------------------------------------------------------- rendering


def _render(result: DeleteSymbolResult, *, format: str, agent: bool) -> None:
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


def _render_agent(result: DeleteSymbolResult) -> None:
    verb = "would delete" if result.status == "dry_run" else "deleted"
    print(f"status: {result.status}")
    print(f"{verb}: {result.qualified_path}  ({result.kind})")
    print(f"file: {result.file_rel}:{result.line_range[0]}-{result.line_range[1]}")
    print(f"lines_removed: {result.lines_removed}")

    if result.callers:
        print()
        print(f"⚠️  {len(result.callers)} reference(s) are now broken:")
        for c in result.callers:
            scope = f" in {c.source_path}" if c.source_path else ""
            print(f"  {c.file}:{c.line}{scope}  ({c.kind})")
        print(f"Fix: symbol callers {result.qualified_path.rsplit('.', 1)[-1]}")
        print(f"Then: symbol patch <each file> to remove or rewrite each call site.")

    if result.diff:
        print()
        print("--- DIFF ---")
        print(result.diff, end="" if result.diff.endswith("\n") else "\n")
        print("--- END ---")


def _render_rich(result: DeleteSymbolResult) -> None:
    color = "green" if result.status == "applied" else "yellow"
    title = "deleted" if result.status == "applied" else "dry run"
    summary = (
        f"[{color}]{title}[/{color}]  "
        f"[bold]{result.qualified_path}[/bold]  "
        f"[dim]({result.kind})  "
        f"{result.file_rel}:{result.line_range[0]}-{result.line_range[1]}  "
        f"-{result.lines_removed} lines[/dim]"
    )
    console.print(summary)

    if result.callers:
        console.print(f"\n[yellow]⚠️  {len(result.callers)} reference(s) now broken[/yellow]")
        t = Table(show_header=True, header_style="bold")
        t.add_column("file:line")
        t.add_column("in symbol")
        t.add_column("kind")
        for c in result.callers:
            t.add_row(
                f"{c.file}:{c.line}",
                c.source_path or "-",
                c.kind,
            )
        console.print(t)
        console.print(
            f"\n[dim]Fix with:[/dim] [bold]symbol patch <file>[/bold] [dim]for each[/dim]"
        )

    if result.diff:
        console.print(
            Panel(
                Syntax(result.diff, "diff", line_numbers=False),
                title="diff",
                border_style="dim",
            )
        )


def _render_error(result: DeleteSymbolResult, *, agent: bool) -> None:
    if agent:
        print(f"status: error")
        print(f"error_code: {result.error_code}")
        print(f"message: {result.message}")
        if result.candidates:
            print("candidates:")
            for c in result.candidates:
                print(f"  {c}")
        if result.callers:
            print(f"callers ({len(result.callers)}):")
            for c in result.callers:
                scope = f" in {c.source_path}" if c.source_path else ""
                print(f"  {c.file}:{c.line}{scope}  ({c.kind})")
        return

    console.print(f"[red]error[/red]  [bold]{result.error_code}[/bold]")
    if result.message:
        console.print(f"  {result.message}")
    if result.candidates:
        console.print("\n[dim]candidates:[/dim]")
        for c in result.candidates:
            console.print(f"  {c}")
    if result.callers:
        console.print(f"\n[yellow]{len(result.callers)} caller(s):[/yellow]")
        t = Table(show_header=True, header_style="bold")
        t.add_column("file:line")
        t.add_column("in symbol")
        t.add_column("kind")
        for c in result.callers:
            t.add_row(f"{c.file}:{c.line}", c.source_path or "-", c.kind)
        console.print(t)
        console.print("\n[dim]Use --force to delete anyway (callers will break).[/dim]")


def _to_dict(result: DeleteSymbolResult) -> dict:
    d = asdict(result)
    return {k: v for k, v in d.items() if v not in (None, "", (), 0, [0, 0])}
