"""`symbol rename-symbol` — CLI + rendering."""

import json as _json
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from wyolet.symbol.shared.symbol_index import get_or_build_index
from wyolet.symbol.writes.rename_symbol import (
    RenameSymbolRequest,
    RenameSymbolResult,
    apply_rename_symbol,
    resolve_rename_symbol,
)

console = Console()


def rename_symbol_cmd(
    *,
    qualified_path: str,
    new_name: str,
    project_root: str = ".",
    dry_run: bool = False,
    agent: bool = False,
    format: str = "rich",
) -> None:
    project = Path(project_root).resolve()
    index, _ = get_or_build_index(project)

    resolved = resolve_rename_symbol(index, qualified_path, new_name, project)
    if isinstance(resolved, RenameSymbolResult):
        _render(resolved, format=format, agent=agent)
        sys.exit(1)

    assert isinstance(resolved, RenameSymbolRequest)
    result = apply_rename_symbol(
        resolved,
        project_root=project,
        dry_run=dry_run,
    )
    _render(result, format=format, agent=agent)
    if result.status == "error":
        sys.exit(1)


def _render(result: RenameSymbolResult, *, format: str, agent: bool) -> None:
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


def _render_agent(result: RenameSymbolResult) -> None:
    verb = "would rename" if result.status == "dry_run" else (
        "needs review" if result.status == "needs_review" else "renamed"
    )
    tier = "ast" if (result.unresolved or result.skipped_mismatch) else "textual"
    leaf = result.qualified_path.rsplit(".", 1)[-1]
    print(f"status: {result.status}")
    print(f"{verb}: {result.qualified_path} → {result.new_qualified_path}")
    print(f"files_changed: {result.files_changed}")
    print(f"refs_updated: {result.refs_updated}")
    print(f"tier: {tier}")
    print()
    for f in result.per_file:
        print(f"  {f.file}  ({f.refs_updated} refs)")
    if result.skipped_mismatch:
        print()
        print(f"skipped {len(result.skipped_mismatch)} site(s) — receiver resolved to a different declaration:")
        for s in result.skipped_mismatch:
            print(f"  {s.file}:{s.line}:{s.col + 1}  `{s.receiver_source}.{leaf}`  → {s.resolved_to_qpath}")
    if result.unresolved:
        print()
        print(f"unresolved {len(result.unresolved)} site(s) — review manually:")
        for u in result.unresolved:
            print(f"  {u.file}:{u.line}:{u.col + 1}  `{u.receiver_source}.{leaf}`  ({u.why})")
    if result.status == "applied":
        print()
        print("undo: symbol undo")


def _render_rich(result: RenameSymbolResult) -> None:
    if result.status == "applied":
        color, title = "green", "renamed"
    elif result.status == "dry_run":
        color, title = "yellow", "dry run"
    else:
        color, title = "magenta", "needs review"
    console.print(
        f"[{color}]{title}[/{color}]  "
        f"[bold]{result.qualified_path}[/bold] → [bold]{result.new_qualified_path}[/bold]  "
        f"[dim]{result.files_changed} files, {result.refs_updated} refs[/dim]"
    )

    if result.per_file:
        t = Table(show_header=True, header_style="bold")
        t.add_column("file")
        t.add_column("refs", justify="right")
        for f in result.per_file:
            t.add_row(f.file, str(f.refs_updated))
        console.print(t)

    leaf = result.qualified_path.rsplit(".", 1)[-1]
    if result.skipped_mismatch:
        console.print(
            f"\n[dim]skipped {len(result.skipped_mismatch)} site(s) "
            f"(receiver bound to a different declaration):[/dim]"
        )
        for s in result.skipped_mismatch:
            console.print(
                f"  [dim]{s.file}:{s.line}:{s.col + 1}[/dim]  "
                f"[yellow]{s.receiver_source}.{leaf}[/yellow] → "
                f"[bold]{s.resolved_to_qpath}[/bold]"
            )
    if result.unresolved:
        console.print(
            f"\n[bold magenta]needs review[/bold magenta] — "
            f"{len(result.unresolved)} site(s) the receiver type "
            f"couldn't be resolved statically:"
        )
        for u in result.unresolved:
            console.print(
                f"  [dim]{u.file}:{u.line}:{u.col + 1}[/dim]  "
                f"[yellow]{u.receiver_source}.{leaf}[/yellow]  [dim]({u.why})[/dim]"
            )

    if result.status == "applied":
        console.print("\n[dim]Undo:[/dim] [bold]symbol undo[/bold]")


def _render_error(result: RenameSymbolResult, *, agent: bool) -> None:
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


def _to_dict(result: RenameSymbolResult) -> dict:
    d = asdict(result)
    return {k: v for k, v in d.items() if v not in (None, "", (), 0)}
