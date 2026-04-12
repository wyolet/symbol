"""Map CLI — visualize import graph architecture."""

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .analyzer import MapResult, analyze_blast, analyze_map

console = Console()


def map_cmd(
    path: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    min_fan_in: int = 5,
    min_fan_out: int = 8,
    min_chain: int = 5,
    blast: str | None = None,
    format: str = "rich",
) -> None:
    """Run the map analysis."""
    project_root = Path(path)
    project_name = project_root.name

    inc = include or None
    exc = exclude or None

    # Blast radius mode — focused analysis on a single file
    if blast:
        _run_blast(project_root, project_name, blast, inc, exc)
        return

    result = analyze_map(project_root, inc, exc, min_fan_in, min_fan_out, min_chain)

    if format == "json":
        data = {
            "total_files": result.total_files,
            "total_edges": result.total_edges,
            "cycles": [{"path": c.path} for c in result.cycles],
            "hotspots": [{"module": h.module, "fan_in": h.fan_in} for h in result.hotspots],
            "fragile": [{"module": f.module, "fan_out": f.fan_out} for f in result.fragile],
            "deep_chains": result.deep_chains,
            "leaves": [{"module": lf.module, "used_by": lf.used_by} for lf in result.leaves],
        }
        print(json.dumps(data, indent=2))
        return

    console.print()
    console.print(Panel(Text(f"  ca map — {project_name}/", style="bold"), style="dim", expand=False))

    # Overview
    console.print()
    console.print(f"  [dim]{result.total_files} files, {result.total_edges} import edges[/dim]")

    # Circular imports
    _print_cycles(result)

    # Hotspots
    _print_hotspots(result)

    # Fragile
    _print_fragile(result)

    # Longest chain
    _print_chain(result)

    # Leaves
    _print_leaves(result)

    console.print()


def _print_cycles(result: MapResult) -> None:
    console.print()
    if result.cycles:
        console.print(Text(f"  \U0001f534 CIRCULAR IMPORTS ({len(result.cycles)})", style="bold red"))
        console.print()
        for cycle in result.cycles:
            arrow_path = " [dim]→[/dim] ".join(f"[bold]{p}[/bold]" for p in cycle.path)
            console.print(f"    [red]✗[/red] {arrow_path}")
    else:
        console.print(Text("  \u2705 CIRCULAR IMPORTS (0)", style="bold green"))
        console.print()
        console.print("    [green]No circular imports detected[/green]")


def _print_hotspots(result: MapResult) -> None:
    console.print()
    if result.hotspots:
        console.print(Text(f"  \U0001f525 HOTSPOTS ({len(result.hotspots)})", style="bold yellow"))
        console.print()
        max_fan = result.hotspots[0].fan_in if result.hotspots else 1
        for h in result.hotspots:
            bar_w = max(1, round(h.fan_in / max_fan * 20))
            bar = f"[yellow]{'█' * bar_w}[/yellow][dim]{'░' * (20 - bar_w)}[/dim]"
            console.print(f"    {bar} [bold]{h.module:<40s}[/bold] [yellow]{h.fan_in} importers[/yellow]")
    else:
        console.print(Text("  \u2705 HOTSPOTS (0)", style="bold green"))
        console.print()
        console.print("    [green]No high fan-in modules[/green]")


def _print_fragile(result: MapResult) -> None:
    console.print()
    if result.fragile:
        console.print(Text(f"  \u26a0\ufe0f  FRAGILE ({len(result.fragile)})", style="bold yellow"))
        console.print()
        max_fan = result.fragile[0].fan_out if result.fragile else 1
        for f in result.fragile:
            bar_w = max(1, round(f.fan_out / max_fan * 20))
            bar = f"[red]{'█' * bar_w}[/red][dim]{'░' * (20 - bar_w)}[/dim]"
            console.print(f"    {bar} [bold]{f.module:<40s}[/bold] [red]imports {f.fan_out} modules[/red]")
    else:
        console.print(Text("  \u2705 FRAGILE (0)", style="bold green"))
        console.print()
        console.print("    [green]No high fan-out modules[/green]")


def _print_chain(result: MapResult) -> None:
    console.print()
    if result.deep_chains:
        console.print(Text(f"  \U0001f517 DEEP CHAINS ({len(result.deep_chains)})", style="bold blue"))
        console.print()
        for chain in result.deep_chains:
            arrow_path = " [dim]\u2192[/dim] ".join(f"[cyan]{p}[/cyan]" for p in chain)
            console.print(f"    [bold]{len(chain)}[/bold] [dim]deep[/dim]  {arrow_path}")
    else:
        console.print(Text("  \u2705 DEEP CHAINS (0)", style="bold green"))
        console.print()
        console.print("    [green]No deep import chains[/green]")


def _print_leaves(result: MapResult) -> None:
    console.print()
    if result.leaves:
        console.print(Text(f"  \U0001f343 LEAF MODULES ({len(result.leaves)})", style="bold blue"))
        console.print()
        for leaf in result.leaves[:15]:  # Cap at 15 to avoid flooding
            console.print(f"    [blue]·[/blue] [bold]{leaf.module:<40s}[/bold] [dim]only used by[/dim] {leaf.used_by}")
        if len(result.leaves) > 15:
            console.print(f"    [dim]... and {len(result.leaves) - 15} more[/dim]")
    else:
        console.print(Text("  \u2705 LEAF MODULES (0)", style="bold green"))
        console.print()
        console.print("    [green]No single-use modules[/green]")


def _run_blast(
    project_root: Path, project_name: str, target: str, inc: list[str] | None, exc: list[str] | None
) -> None:
    """Run blast radius analysis for a single file."""
    result = analyze_blast(project_root, target, inc, exc)

    console.print()
    console.print(Panel(Text(f"  ca map --blast — {project_name}/", style="bold"), style="dim", expand=False))
    console.print()

    if not result.direct and not result.transitive:
        console.print(f"  [yellow]No dependents found for[/yellow] [bold]{target}[/bold]")
        console.print("  [dim](file not found or nothing imports it)[/dim]")
        console.print()
        return

    console.print(f"  [bold]Target:[/bold] [cyan]{result.target}[/cyan]")
    console.print(f"  [bold]Blast radius:[/bold] [red]{result.total} files affected[/red]")
    console.print()

    # Direct dependents
    console.print(Text(f"  \U0001f534 DIRECT ({len(result.direct)})", style="bold red"))
    console.print()
    for dep in result.direct:
        console.print(f"    [red]\u2717[/red] {dep}")

    # Transitive dependents
    if result.transitive:
        console.print()
        console.print(Text(f"  \U0001f7e0 TRANSITIVE ({len(result.transitive)})", style="bold yellow"))
        console.print()
        for dep in result.transitive:
            console.print(f"    [yellow]![/yellow] {dep}")

    console.print()
