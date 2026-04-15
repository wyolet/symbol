"""`ca code` — retrieve the body at a known address."""

import json as _json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from ca_tools.queries.code import CodeAmbiguous, CodeNotFound, code as code_query
from ca_tools.shared.symbol_index import get_or_build_index

console = Console()


def code_cmd(
    target: str,
    path: str = ".",
    include_refs: bool = True,
    format: str = "rich",
) -> None:
    project_root = Path(path).resolve()
    index, _source = get_or_build_index(project_root)

    try:
        hit = code_query(index, target)
    except CodeNotFound as e:
        if format == "json":
            print(_json.dumps({"error": "not_found", "message": str(e)}))
        else:
            console.print(f"[yellow]{e}[/yellow]")
            console.print("[dim]Try: ca search <name>[/dim]")
        return
    except CodeAmbiguous as e:
        if format == "json":
            print(_json.dumps({"error": "ambiguous", "candidates": e.candidates}, indent=2))
            return
        console.print(
            f"[yellow]{len(e.candidates)} matches — pass file:start-end or a longer path[/yellow]\n"
        )
        table = Table(show_header=True, header_style="bold")
        table.add_column("signature")
        table.add_column("location")
        for c in e.candidates:
            table.add_row(c["signature"], f"{c['file']}:{c['start_line']}-{c['end_line']}")
        console.print(table)
        return

    if format == "json":
        print(_json.dumps(hit, indent=2))
        return

    header = (
        f"[bold cyan]{hit['path'] or hit['kind']}[/bold cyan]  "
        f"[dim]{hit['file']}:{hit['start_line']}-{hit['end_line']}  ({hit['kind']})[/dim]"
    )
    console.print(header)
    if hit.get("note"):
        console.print(f"[dim]{hit['note']}[/dim]")
    console.print(
        Panel(
            Syntax(hit["body"], hit["language"], line_numbers=True, start_line=hit["start_line"]),
            border_style="dim",
        )
    )
    if hit["imports"]:
        console.print("[bold]imports[/bold]")
        for imp in hit["imports"]:
            origin = f"from {imp['source']}" if imp["source"] else ""
            console.print(f"  [green]{imp['name']}[/green] {origin} [dim]:{imp['line']}[/dim]")
    if include_refs and hit["refs"]:
        console.print(f"[bold]refs[/bold] [dim]({len(hit['refs'])} external names)[/dim]")
        for ref in hit["refs"][:30]:
            tag = "" if ref["kind"] == "name" else "."
            console.print(f"  {tag}{ref['name']} [dim]:{ref['line']}[/dim]")
        if len(hit["refs"]) > 30:
            console.print(f"  [dim]... {len(hit['refs']) - 30} more[/dim]")
