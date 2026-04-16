"""`ca callers` — tier-1 textual reference scan."""

import json as _json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ca_tools.reads.callers import callers as callers_query
from ca_tools.shared.symbol_index import get_or_build_index

console = Console()


def callers_cmd(name: str, path: str = ".", format: str = "rich") -> None:
    project_root = Path(path).resolve()
    index, _source = get_or_build_index(project_root)

    hits = callers_query(index, name)

    if format == "json":
        print(_json.dumps(hits, indent=2))
        return

    if not hits:
        console.print(f"[yellow]No references found for[/yellow] {name!r}")
        return

    console.print(
        f"[dim]{len(hits)} tier-1 unresolved refs — name-match only, "
        f"may include unrelated symbols sharing the name[/dim]\n"
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("caller")
    table.add_column("file:line")
    table.add_column("kind")
    for h in hits[:200]:
        table.add_row(
            h["source_path"],
            f"{h['source_file']}:{h['ref_line']}",
            h["ref_kind"],
        )
    console.print(table)
    if len(hits) > 200:
        console.print(f"[dim]... {len(hits) - 200} more (use --format json to see all)[/dim]")
