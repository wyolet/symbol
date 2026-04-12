"""LOC CLI — GitHub Linguist-powered lines of code counter."""

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .linguist import Linguist

console = Console()

BAR_WIDTH = 60


def _language_bar(sorted_langs: list[dict], total_lines: int) -> Text:
    """Render a GitHub-style colored language bar."""
    bar = Text()
    if not total_lines:
        bar.append("░" * BAR_WIDTH, style="dim")
        return bar

    remaining = BAR_WIDTH
    for i, ls in enumerate(sorted_langs):
        if ls["lines"] == 0:
            continue
        color = ls.get("color") or "#888888"
        if i == len(sorted_langs) - 1:
            width = remaining  # last language gets whatever is left
        else:
            width = max(1, round(ls["lines"] / total_lines * BAR_WIDTH))
            width = min(width, remaining)
        remaining -= width
        if width > 0:
            bar.append("█" * width, style=color)
        if remaining <= 0:
            break

    return bar


def _language_legend(sorted_langs: list[dict]) -> Text:
    """Render colored dots with language names and percentages under the bar."""
    legend = Text()
    shown = 0
    for ls in sorted_langs:
        pct = ls.get("percentage_lines", 0)
        if not ls.get("is_counted") or pct == 0:
            continue
        if shown > 0:
            legend.append("   ")
        color = ls.get("color") or "#888888"
        legend.append("● ", style=color)
        legend.append(f"{ls['name']} ", style="bold")
        legend.append(f"{pct:.1f}%", style="dim")
        shown += 1
    return legend


def loc_cmd(path: str, format: str = "rich") -> None:
    """Run the LOC counter."""
    project_root = Path(path)
    project_name = project_root.name

    linguist = Linguist()
    stats = linguist.detect_directory(str(project_root))

    if format == "json":
        print(json.dumps(stats, indent=2))
        return

    console.print()
    console.print(Panel(Text(f"  ca loc — {project_name}/", style="bold"), style="dim", expand=False))

    if not stats:
        console.print()
        console.print("  [dim](no recognized source files)[/dim]")
        console.print()
        return

    sorted_langs = sorted(stats, key=lambda s: s["lines"], reverse=True)
    total_lines = sum(s["lines"] for s in stats)
    total_files = sum(s["files"] for s in stats)
    total_size = sum(s["size"] for s in stats)

    # GitHub-style language bar
    console.print()
    console.print("  ", end="")
    console.print(_language_bar(sorted_langs, total_lines))
    console.print("  ", end="")
    console.print(_language_legend(sorted_langs))

    # Detail table
    console.print()
    max_lines = sorted_langs[0]["lines"] if sorted_langs else 1

    table = Table(box=None, padding=(0, 2, 0, 2), show_edge=False)
    table.add_column("Language", style="bold", min_width=18)
    table.add_column("Type", style="dim", min_width=12)
    table.add_column("Files", justify="right", style="cyan")
    table.add_column("Lines", justify="right", style="green")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("%", justify="right", style="yellow")
    table.add_column("", min_width=25)

    for ls in sorted_langs:
        bar_width = int((ls["lines"] / max_lines) * 25) if max_lines else 0
        color = ls.get("color") or "#888888"
        bar = f"[{color}]{'█' * bar_width}[/{color}][dim]{'░' * (25 - bar_width)}[/dim]"
        pct = f"{ls['percentage_lines']:.1f}%" if ls.get("is_counted") and ls["percentage_lines"] else ""
        size_kb = f"{ls['size'] / 1024:.0f}K"
        table.add_row(ls["name"], ls.get("type", ""), str(ls["files"]), str(ls["lines"]), size_kb, pct, bar)

    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        "",
        f"[bold]{total_files}[/bold]",
        f"[bold green]{total_lines}[/bold green]",
        f"[bold dim]{total_size / 1024:.0f}K[/bold dim]",
        "",
        "",
    )

    console.print(table)
    console.print()
