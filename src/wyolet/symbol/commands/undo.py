"""`symbol undo` — revert the most recent symbol-write transaction."""

import json as _json
import sys
from pathlib import Path

from rich.console import Console

from wyolet.symbol.writes.undo import UndoResult, undo_last


console = Console()


def undo_cmd(
    *,
    project_root: str = ".",
    agent: bool = False,
    format: str = "rich",
) -> None:
    project = Path(project_root).resolve()
    result = undo_last(project)
    _render(result, format=format, agent=agent)
    if result.status == "error":
        sys.exit(1)


def _render(result: UndoResult, *, format: str, agent: bool) -> None:
    if format == "json":
        print(_json.dumps(_to_dict(result), indent=2))
        return
    if agent:
        _render_agent(result)
    else:
        _render_rich(result)


def _render_agent(r: UndoResult) -> None:
    print(f"status: {r.status}")
    if r.transaction_id:
        print(f"transaction_id: {r.transaction_id}")
    if r.op:
        print(f"op: {r.op}")
    if r.subject:
        print(f"subject: {r.subject}")
    if r.files_restored:
        print(f"restored: {len(r.files_restored)}")
        for f in r.files_restored:
            print(f"  - {f}")
    if r.files_skipped:
        print(f"skipped: {len(r.files_skipped)}")
        for f in r.files_skipped:
            print(f"  - {f}")
    if r.error_code:
        print(f"error_code: {r.error_code}")
    if r.message:
        print(f"message: {r.message}")


def _render_rich(r: UndoResult) -> None:
    if r.status == "nothing_to_undo":
        console.print("[dim]nothing to undo[/dim]")
        return
    if r.status == "error":
        console.print(f"[red]undo failed[/red]  [bold]{r.error_code}[/bold]")
        if r.message:
            console.print(f"  {r.message}")
        return
    console.print(f"[green]undone[/green]  [dim]{r.op}[/dim]  {r.subject or ''}")
    console.print(f"[dim]restored {len(r.files_restored)} file(s)[/dim]")
    for f in r.files_restored:
        console.print(f"  [dim]•[/dim] {f}")
    if r.files_skipped:
        console.print(f"[yellow]skipped {len(r.files_skipped)} file(s) (I/O error)[/yellow]")


def _to_dict(r: UndoResult) -> dict:
    return {
        "status": r.status,
        "transaction_id": r.transaction_id,
        "op": r.op,
        "subject": r.subject,
        "files_restored": list(r.files_restored),
        "files_skipped": list(r.files_skipped),
        "error_code": r.error_code,
        "message": r.message,
    }
