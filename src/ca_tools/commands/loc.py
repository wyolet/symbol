"""LOC CLI — GitHub Linguist-powered lines of code counter."""

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ca_tools.shared.linguist import Linguist
from ca_tools.shared.project_config import load_project_config
from ca_tools.shared.spec import load_spec

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


def loc_cmd(path: str, format: str = "rich") -> None:
    """Run the LOC counter."""
    project_root = Path(path)
    project_name = project_root.name

    config = load_project_config(project_root)
    spec = load_spec(project_root=project_root)

    scanner_exclude = list(spec.scanner.exclude) + config.scanner.exclude

    linguist = Linguist()
    stats = linguist.detect_directory(str(project_root), exclude=scanner_exclude or None)

    if format == "json":
        # Enrich JSON with summary and biggest files
        all_files = linguist.all_files()
        code_files = []
        for lang_stat in linguist.statistics.values():
            if lang_stat.type == "programming":
                for f in lang_stat.file_stats:
                    code_files.append({"path": f.path, "lines": f.lines, "sloc": f.sloc, "size": f.size, "language": lang_stat.name})
        biggest = sorted(code_files, key=lambda x: x["lines"], reverse=True)[:10]
        data = {
            "summary": {
                "sloc": sum(s["sloc"] for s in stats),
                "loc": sum(s["loc"] for s in stats),
                "files": sum(s["files"] for s in stats),
                "size": sum(s["size"] for s in stats),
                "languages": len(stats),
                "code_languages": sum(1 for s in stats if s.get("type") == "programming"),
            },
            "languages": stats,
            "biggest_files": biggest,
        }
        print(json.dumps(data, indent=2))
        return

    console.print()
    console.print(Panel(Text(f"  ca loc — {project_name}/", style="bold"), style="dim", expand=False))

    if not stats:
        console.print()
        console.print("  [dim](no recognized source files)[/dim]")
        console.print()
        return

    sorted_langs = sorted(stats, key=lambda s: s["sloc"], reverse=True)
    total_sloc = sum(s["sloc"] for s in stats)
    total_loc = sum(s["loc"] for s in stats)
    total_files = sum(s["files"] for s in stats)
    total_size = sum(s["size"] for s in stats)
    lang_count = len(sorted_langs)
    code_langs = sum(1 for s in stats if s.get("type") == "programming")
    avg_file = total_loc // total_files if total_files else 0
    blank_lines = total_loc - total_sloc

    # GitHub-style language bar
    console.print()
    console.print("  ", end="")
    console.print(_language_bar(sorted_langs, total_sloc))

    # Summary metrics
    console.print()
    console.print(f"  [bold]{total_sloc:,}[/bold] [dim]sloc[/dim]  "
                  f"[bold]{total_loc:,}[/bold] [dim]lines[/dim]  "
                  f"[bold]{blank_lines:,}[/bold] [dim]blank[/dim]  "
                  f"[bold]{total_files:,}[/bold] [dim]files[/dim]  "
                  f"[bold]{avg_file}[/bold] [dim]avg lines/file[/dim]  "
                  f"[bold]{code_langs}[/bold][dim]/{lang_count} langs[/dim]")

    # Detail table
    console.print()

    table = Table(box=None, padding=(0, 2, 0, 2), show_edge=False)
    table.add_column("Language", min_width=22)
    table.add_column("Type", style="dim", min_width=12)
    table.add_column("Files", justify="right", style="cyan")
    table.add_column("SLOC", justify="right", style="green")
    table.add_column("Lines", justify="right", style="dim")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("%", justify="right", style="yellow")

    for ls in sorted_langs:
        color = ls.get("color") or "#888888"
        pct = f"{ls['percentage_lines']:.1f}%" if ls.get("is_counted") and ls["percentage_lines"] else ""
        size_kb = f"{ls['size'] / 1024:.0f}K"
        name = Text()
        name.append("● ", style=color)
        name.append(ls["name"], style="bold")
        table.add_row(name, ls.get("type", ""), str(ls["files"]), str(ls["sloc"]), str(ls["loc"]), size_kb, pct)

    table.add_section()
    table.add_row(
        Text("  Total", style="bold"),
        "",
        f"[bold]{total_files}[/bold]",
        f"[bold green]{total_sloc:,}[/bold green]",
        f"[bold]{total_loc:,}[/bold]",
        f"[bold dim]{total_size / 1024:.0f}K[/bold dim]",
        "",
    )

    # Biggest files — only programming languages
    console.print(table)
    code_files = []
    for lang_stat in linguist.statistics.values():
        if lang_stat.type == "programming":
            for f in lang_stat.file_stats:
                code_files.append((f, lang_stat.name, lang_stat.color))
    if code_files:
        biggest = sorted(code_files, key=lambda x: x[0].lines, reverse=True)[:5]
        console.print()
        console.print(Text("  📄 BIGGEST FILES", style="bold"))
        console.print()
        abs_root = str(project_root.resolve())
        for f, lang_name, color in biggest:
            rel = f.path.removeprefix(abs_root).lstrip("/")
            console.print(f"    [bold]{rel:<50s}[/bold] [green]{f.lines:,}[/green] [dim]lines[/dim]  [{color or '#888'}]●[/{color or '#888'}] [dim]{lang_name}[/dim]")

    console.print()
