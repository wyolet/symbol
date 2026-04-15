"""`ca search` — narrow candidates by name, return signatures + previews."""

import json as _json
from pathlib import Path

from rich.console import Console

from ca_tools.queries.search import search as search_query
from ca_tools.shared.symbol_index import get_or_build_index

console = Console()


def search_cmd(
    patterns: list[str],
    path: str = ".",
    kind: str | None = None,
    file: str | None = None,
    regex: bool = False,
    fixed: bool = False,
    ignore_case: bool = False,
    limit: int = 100,
    format: str = "rich",
) -> None:
    if regex and fixed:
        console.print("[red]--regex and --fixed are mutually exclusive[/red]")
        raise SystemExit(2)

    project_root = Path(path).resolve()
    index, _source = get_or_build_index(project_root)

    hits = search_query(
        index,
        patterns,
        kind=kind,
        file=file,
        regex=regex,
        fixed=fixed,
        ignore_case=ignore_case,
        limit=limit,
    )

    if format == "json":
        print(_json.dumps(hits, indent=2))
        return

    if not hits:
        console.print(f"[yellow]No matches for[/yellow] {' '.join(patterns)!r}")
        return

    console.print(f"[dim]{len(hits)} match{'es' if len(hits) != 1 else ''}[/dim]\n")
    sep = "[dim]" + ("─" * 78) + "[/dim]"
    for i, h in enumerate(hits):
        if i > 0:
            console.print(sep)
        sig = h["signature"] or h["path"]
        location = f"{h['file']}:{h['start_line']}-{h['end_line']}"
        console.print(f"[dim]{location}[/dim]")
        console.print(f"[bold]{sig}[/bold]")
    console.print(f"\n[dim]Use[/dim] [bold]ca code <file:start-end>[/bold] [dim]to fetch a body.[/dim]")
