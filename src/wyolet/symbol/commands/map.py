"""Map CLI — visualize import graph architecture."""

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from wyolet.symbol.shared.findings import Severity
from wyolet.symbol.shared.project_config import MapSeverityFilter, load_project_config

from wyolet.symbol.shared.graph import MapResult, analyze_blast, analyze_map

_SEV_ORDER = {
    Severity.DEBUG: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
    Severity.CRITICAL: 4,
}

console = Console()

# Consistent indentation
I1 = "  "    # section headers, overview
I2 = "    "  # items, descriptions
I3 = "      "  # sub-items (cycle reasons, etc.)

_SEV_STYLE = {
    Severity.DEBUG: "dim",
    Severity.INFO: "blue",
    Severity.WARNING: "yellow",
    Severity.ERROR: "red",
    Severity.CRITICAL: "bold red",
}

_SEV_ICON = {
    Severity.DEBUG: "\u00b7",
    Severity.INFO: "\u00b7",
    Severity.WARNING: "!",
    Severity.ERROR: "\u2717",
    Severity.CRITICAL: "\u2718",
}


def map_cmd(
    path: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    blast: str | None = None,
    severity: str | None = None,
    coupling_depth: int = 1,
    show: str | None = None,
    limit: int | None = None,
    format: str = "rich",
) -> None:
    """Run the map analysis."""
    project_root = Path(path)
    project_name = project_root.name

    config = load_project_config(project_root)
    inc = include or config.checker.include or None
    exc = exclude or config.checker.exclude or None

    # CLI --severity overrides pyproject general level
    sev_filter = config.map_severity
    if severity:
        sev_filter.general = Severity(severity.lower())

    # CLI --limit overrides pyproject default
    cap = limit if limit is not None else config.map_limit

    # Blast radius mode — focused analysis on a single file
    if blast:
        _run_blast(project_root, project_name, blast, inc, exc)
        return

    result = analyze_map(project_root, inc, exc, thresholds=config.map_thresholds, coupling_depth=coupling_depth)

    if format == "json":
        min_cycles = sev_filter.for_section("cycles")
        min_hotspots = sev_filter.for_section("hotspots")
        min_fragile = sev_filter.for_section("fragile")
        min_chains = sev_filter.for_section("deep_chains")
        data = {
            "total_files": result.total_files,
            "total_edges": result.total_edges,
            "cycles": [{"path": c.path, "severity": c.severity.value, "failed_name": c.failed_name, "reason": c.reason, "trigger_line": c.trigger_line} for c in result.cycles if _at_least(c.severity, min_cycles)],
            "hotspots": [{"module": h.module, "fan_in": h.fan_in, "severity": h.severity.value} for h in result.hotspots if _at_least(h.severity, min_hotspots)],
            "fragile": [{"module": f.module, "fan_out": f.fan_out, "severity": f.severity.value} for f in result.fragile if _at_least(f.severity, min_fragile)],
            "deep_chains": [{"chain": dc.chain, "severity": dc.severity.value} for dc in result.deep_chains if _at_least(dc.severity, min_chains)],
            "leaves": [{"module": lf.module, "used_by": lf.used_by, "lines": lf.lines} for lf in result.leaves],
            "coupling": [{"name": n.name, "deps": n.deps, "mutual": n.mutual, "children": [{"name": c.name, "deps": c.deps} for c in n.children]} for n in result.coupling],
        }
        print(json.dumps(data, indent=2))
        return

    console.print()
    console.print(Panel(Text(f"{I1}symbol map — {project_name}/", style="bold"), style="dim", expand=False))

    # Overview
    console.print()
    console.print(f"{I1}[dim]{result.total_files} files, {result.total_edges} import edges[/dim]")

    # --show mode: full detail for one section
    if show:
        no_cap = 0
        sections = {
            "cycles": lambda: _print_cycles(result, sev_filter, no_cap),
            "hotspots": lambda: _print_hotspots(result, sev_filter, no_cap),
            "fragile": lambda: _print_fragile(result, sev_filter, no_cap),
            "chains": lambda: _print_chain(result, sev_filter, no_cap),
            "leafs": lambda: _print_leaves(result, sev_filter, no_cap),
            "coupling": lambda: _print_coupling(result),
        }
        if show in sections:
            sections[show]()
        else:
            console.print(f"\n{I1}[red]Unknown section '{show}'. Choose from: {', '.join(sections)}[/red]")
        console.print()
        return

    # Summary mode: capped output
    _print_cycles(result, sev_filter, cap)
    _print_hotspots(result, sev_filter, cap)
    _print_fragile(result, sev_filter, cap)
    _print_chain(result, sev_filter, cap)
    _print_leaves(result, sev_filter, cap)
    _print_coupling(result)

    console.print()


def _at_least(item_sev: Severity, min_sev: Severity) -> bool:
    return _SEV_ORDER[item_sev] >= _SEV_ORDER[min_sev]


def _print_cycles(result: MapResult, sev_filter: MapSeverityFilter, cap: int = 0) -> None:
    min_sev = sev_filter.for_section("cycles")
    visible = [c for c in result.cycles if _at_least(c.severity, min_sev)]
    console.print()
    if visible:
        crits = sum(1 for c in visible if c.severity == Severity.CRITICAL)
        errs = sum(1 for c in visible if c.severity == Severity.ERROR)
        warns = sum(1 for c in visible if c.severity == Severity.WARNING)
        header_style = "bold red" if (crits or errs) else "bold yellow" if warns else "bold blue"
        total_label = f" [dim]({len(result.cycles)} total)[/dim]" if len(visible) < len(result.cycles) else ""
        console.print(Text(f"{I1}\U0001f534 CIRCULAR IMPORTS ({len(visible)})", style=header_style), total_label, end="")
        console.print()
        console.print(f"{I2}[dim]Imports that would fail at runtime — Python can't resolve these[/dim]")
        console.print()
        shown = visible if not cap else visible[:cap]
        for cycle in shown:
            color = _SEV_STYLE[cycle.severity]
            icon = _SEV_ICON[cycle.severity]
            arrow_path = " [dim]→[/dim] ".join(f"[bold]{p}[/bold]" for p in cycle.path)
            console.print(f"{I2}[{color}]{icon}[/{color}] {arrow_path}")
            if cycle.failed_name:
                console.print(f"{I3}[dim]{cycle.reason}[/dim]")
        if cap and len(visible) > cap:
            console.print(f"{I2}[dim]... and {len(visible) - cap} more — use --show cycles for full list[/dim]")
    else:
        console.print(Text(f"{I1}\u2705 CIRCULAR IMPORTS (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No circular imports detected[/green]")


def _print_hotspots(result: MapResult, sev_filter: MapSeverityFilter, cap: int = 0) -> None:
    min_sev = sev_filter.for_section("hotspots")
    visible = [h for h in result.hotspots if _at_least(h.severity, min_sev)]
    console.print()
    if visible:
        total_label = f" [dim]({len(result.hotspots)} total)[/dim]" if len(visible) < len(result.hotspots) else ""
        console.print(Text(f"{I1}\U0001f525 HOTSPOTS ({len(visible)})", style="bold yellow"), total_label, end="")
        console.print()
        console.print(f"{I2}[dim]Most imported modules — changes here ripple everywhere[/dim]")
        console.print()
        shown = visible if not cap else visible[:cap]
        max_fan = visible[0].fan_in if visible else 1
        for h in shown:
            color = _SEV_STYLE[h.severity]
            bar_w = max(1, round(h.fan_in / max_fan * 20))
            bar = f"[{color}]{'█' * bar_w}[/{color}][dim]{'░' * (20 - bar_w)}[/dim]"
            console.print(f"{I2}{bar} [bold]{h.module:<40s}[/bold] [{color}]{h.fan_in} importers[/{color}]")
        if cap and len(visible) > cap:
            console.print(f"{I2}[dim]... and {len(visible) - cap} more — use --show hotspots for full list[/dim]")
    else:
        console.print(Text(f"{I1}\u2705 HOTSPOTS (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No high fan-in modules[/green]")


def _print_fragile(result: MapResult, sev_filter: MapSeverityFilter, cap: int = 0) -> None:
    min_sev = sev_filter.for_section("fragile")
    visible = [f for f in result.fragile if _at_least(f.severity, min_sev)]
    console.print()
    if visible:
        total_label = f" [dim]({len(result.fragile)} total)[/dim]" if len(visible) < len(result.fragile) else ""
        console.print(Text(f"{I1}\u26a0\ufe0f  FRAGILE ({len(visible)})", style="bold yellow"), total_label, end="")
        console.print()
        console.print(f"{I2}[dim]Modules importing too many things — high blast radius if they break[/dim]")
        console.print()
        shown = visible if not cap else visible[:cap]
        max_fan = visible[0].fan_out if visible else 1
        for f in shown:
            color = _SEV_STYLE[f.severity]
            bar_w = max(1, round(f.fan_out / max_fan * 20))
            bar = f"[{color}]{'█' * bar_w}[/{color}][dim]{'░' * (20 - bar_w)}[/dim]"
            console.print(f"{I2}{bar} [bold]{f.module:<40s}[/bold] [{color}]imports {f.fan_out} modules[/{color}]")
        if cap and len(visible) > cap:
            console.print(f"{I2}[dim]... and {len(visible) - cap} more — use --show fragile for full list[/dim]")
    else:
        console.print(Text(f"{I1}\u2705 FRAGILE (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No high fan-out modules[/green]")


def _print_chain(result: MapResult, sev_filter: MapSeverityFilter, cap: int = 0) -> None:
    min_sev = sev_filter.for_section("deep_chains")
    visible = [dc for dc in result.deep_chains if _at_least(dc.severity, min_sev)]
    console.print()
    if visible:
        total_label = f" [dim]({len(result.deep_chains)} total)[/dim]" if len(visible) < len(result.deep_chains) else ""
        console.print(Text(f"{I1}\U0001f517 DEEP CHAINS ({len(visible)})", style="bold blue"), total_label, end="")
        console.print()
        console.print(f"{I2}[dim]Long import chains — many layers between entry point and leaf[/dim]")
        console.print()
        shown = visible if not cap else visible[:cap]
        for dc in shown:
            color = _SEV_STYLE[dc.severity]
            arrow_path = " [dim]\u2192[/dim] ".join(f"[{color}]{p}[/{color}]" for p in dc.chain)
            console.print(f"{I2}[{color}][bold]{len(dc.chain)}[/bold] deep[/{color}]  {arrow_path}")
        if cap and len(visible) > cap:
            console.print(f"{I2}[dim]... and {len(visible) - cap} more — use --show chains for full list[/dim]")
    else:
        console.print(Text(f"{I1}\u2705 DEEP CHAINS (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No deep import chains[/green]")


def _print_leaves(result: MapResult, sev_filter: MapSeverityFilter, cap: int = 0) -> None:
    min_sev = sev_filter.for_section("leaves")
    if _SEV_ORDER[min_sev] > _SEV_ORDER[Severity.INFO]:
        return
    console.print()
    if result.leaves:
        console.print(Text(f"{I1}\U0001f343 SMALL LEAFS ({len(result.leaves)})", style="bold blue"))
        console.print(f"{I2}[dim]Single-use modules under 100 lines — consider inlining[/dim]")
        console.print()
        shown = result.leaves if not cap else result.leaves[:cap]
        for leaf in shown:
            lines_str = f"[yellow]{leaf.lines}[/yellow]" if leaf.lines < 30 else f"[dim]{leaf.lines}[/dim]"
            console.print(f"{I2}[blue]\u00b7[/blue] [bold]{leaf.module:<40s}[/bold] {lines_str:>3s} lines  [dim]\u2192[/dim] {leaf.used_by}")
        if cap and len(result.leaves) > cap:
            console.print(f"{I2}[dim]... and {len(result.leaves) - cap} more — use --show leafs for full list[/dim]")
    else:
        console.print(Text(f"{I1}\u2705 SMALL LEAFS (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No small single-use modules[/green]")


def _print_coupling(result: MapResult) -> None:
    if not result.coupling:
        return
    console.print()
    total_edges = sum(len(n.deps) + len(n.mutual) for n in result.coupling)
    console.print(Text(f"{I1}\U0001f4e6 MODULE COUPLING ({total_edges} edges)", style="bold cyan"))
    console.print(f"{I2}[dim]Which packages depend on which — [red]\u2194[/red] = mutual dependency[/dim]")
    console.print()

    tree = Tree("[dim].[/dim]", guide_style="dim")

    for node in result.coupling:
        label_parts: list[str] = [f"[cyan bold]{node.name}[/cyan bold]"]
        if node.mutual:
            mutual_str = ", ".join(f"[red bold]{m}[/red bold]" for m in node.mutual)
            label_parts.append(f"[red]\u2194[/red] {mutual_str}")
        if node.deps:
            deps_str = ", ".join(f"[bold]{d}[/bold]" for d in node.deps)
            label_parts.append(f"[dim]\u2192[/dim] {deps_str}")

        branch = tree.add("  ".join(label_parts))

        for child in node.children:
            child_deps = ", ".join(f"[bold]{d}[/bold]" for d in child.deps)
            short_name = child.name.split("/", 1)[1] if "/" in child.name else child.name
            branch.add(f"[dim]{short_name}[/dim]  [dim]\u2192[/dim] {child_deps}")

    console.print(tree)


def _run_blast(
    project_root: Path, project_name: str, target: str, inc: list[str] | None, exc: list[str] | None
) -> None:
    """Run blast radius analysis for a single file."""
    result = analyze_blast(project_root, target, inc, exc)

    console.print()
    console.print(Panel(Text(f"{I1}symbol map --blast — {project_name}/", style="bold"), style="dim", expand=False))
    console.print()

    if not result.direct and not result.transitive:
        console.print(f"{I1}[yellow]No dependents found for[/yellow] [bold]{target}[/bold]")
        console.print(f"{I1}[dim](file not found or nothing imports it)[/dim]")
        console.print()
        return

    console.print(f"{I1}[bold]Target:[/bold] [cyan]{result.target}[/cyan]")
    console.print(f"{I1}[bold]Blast radius:[/bold] [red]{result.total} files affected[/red]")
    console.print()

    console.print(Text(f"{I1}\U0001f534 DIRECT ({len(result.direct)})", style="bold red"))
    console.print()
    for dep in result.direct:
        console.print(f"{I2}[red]\u2717[/red] {dep}")

    if result.transitive:
        console.print()
        console.print(Text(f"{I1}\U0001f7e0 TRANSITIVE ({len(result.transitive)})", style="bold yellow"))
        console.print()
        for dep in result.transitive:
            console.print(f"{I2}[yellow]![/yellow] {dep}")

    console.print()
