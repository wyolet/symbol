"""`ca search` — narrow candidates by name, return signatures + previews."""

import json as _json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ca_tools.queries.search import search as search_query
from ca_tools.shared.symbol_index import get_or_build_index

console = Console()


def search_cmd(
    query: str,
    path: str = ".",
    kind: str | None = None,
    file: str | None = None,
    limit: int = 100,
    format: str = "rich",
) -> None:
    project_root = Path(path).resolve()
    index, _source = get_or_build_index(project_root)

    hits = search_query(index, query, kind=kind, file=file, limit=limit)

    if format == "json":
        print(_json.dumps(hits, indent=2))
        return

    if not hits:
        console.print(f"[yellow]No matches for[/yellow] {query!r}")
        return

    console.print(f"[dim]{len(hits)} match{'es' if len(hits) != 1 else ''}[/dim]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("signature")
    table.add_column("location")
    table.add_column("preview", overflow="fold")
    for h in hits:
        sig = h["signature"] or h["path"]
        location = f"{h['file']}:{h['start_line']}-{h['end_line']}"
        table.add_row(sig, location, h["preview"])
    console.print(table)
    console.print(
        f"\n[dim]Use[/dim] [bold]ca code {hits[0]['path']}[/bold] "
        f"[dim]or[/dim] [bold]ca code {hits[0]['file']}:{hits[0]['start_line']}-{hits[0]['end_line']}[/bold] "
        "[dim]to fetch a body.[/dim]"
    )
