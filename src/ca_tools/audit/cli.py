"""Audit CLI — detect stack, entry points, orphans, side effects."""

import fnmatch
import json
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ca_tools.shared.findings import SEVERITY_STYLE, Report, Severity
from ca_tools.shared.project_config import load_project_config
from ca_tools.shared.spec import load_spec

from .config import detect_config_files
from .entrypoints import EntryPoint, detect_entrypoints
from .orphans import build_import_graph, detect_orphans, graph_summary
from .sideeffects import SideEffect, detect_sideeffects
from .stack import detect_deps, detect_stack
from .unused_deps import detect_unused_deps

console = Console()


def _section_header(title: str, count: int | None, severity: Severity | None) -> None:
    console.print()
    if severity is None or count is None:
        icon = {"stack": "\U0001f4e6", "config": "\U0001f4c4", "graph": "\U0001f517", "entry": "\U0001f680"}.get(
            title.split()[0].lower(), "\U0001f4c4"
        )
        console.print(Text(f"  {icon} {title}", style="bold blue"))
    elif count == 0:
        console.print(Text(f"  \u2705 {title}", style="bold green"))
    else:
        style_map = {Severity.ERROR: "bold red", Severity.WARNING: "bold yellow", Severity.INFO: "bold blue"}
        icon_map = {Severity.ERROR: "\U0001f534", Severity.WARNING: "\u26a0\ufe0f ", Severity.INFO: "\u2139\ufe0f "}
        console.print(Text(f"  {icon_map[severity]} {title}", style=style_map[severity]))
    console.print()


def _finding_line(location: str, message: str, severity: Severity) -> None:
    style, icon = SEVERITY_STYLE[severity]
    console.print(f"    [{style}]{icon}[/{style}] [bold]{location:<30s}[/bold] {message}")


def _health_bar(connected: int, orphans: int, total: int, width: int = 50) -> str:
    if total == 0:
        return "[dim]" + "░" * width + "[/dim]"
    green_w = max(1, round(connected / total * width)) if connected else 0
    red_w = max(1, round(orphans / total * width)) if orphans else 0
    if green_w + red_w > width:
        red_w = width - green_w
    gap = width - green_w - red_w
    return f"[green]{'█' * green_w}[/green][red]{'█' * red_w}[/red][dim]{'░' * gap}[/dim]"


def _severity_bar(errors: int, warnings: int, infos: int, width: int = 40) -> str:
    total = errors + warnings + infos
    if total == 0:
        return "[green]" + "█" * width + "[/green]"
    red_w = max(1, round(errors / total * width)) if errors else 0
    yellow_w = max(1, round(warnings / total * width)) if warnings else 0
    blue_w = width - red_w - yellow_w
    if blue_w < 0:
        blue_w = 0
        yellow_w = width - red_w
    return f"[red]{'█' * red_w}[/red][yellow]{'█' * yellow_w}[/yellow][blue]{'█' * blue_w}[/blue]"


def _print_summary(report: Report) -> None:
    console.print()
    parts: list[str] = []
    if report.errors:
        parts.append(f"[bold red]{report.errors} error{'s' if report.errors != 1 else ''}[/bold red]")
    if report.warnings:
        parts.append(f"[bold yellow]{report.warnings} warning{'s' if report.warnings != 1 else ''}[/bold yellow]")
    if report.infos:
        parts.append(f"[bold blue]{report.infos} info[/bold blue]")

    if not parts:
        console.print(Panel("[bold green]\u2705 No issues found[/bold green]", style="green", expand=False))
    else:
        bar = _severity_bar(report.errors, report.warnings, report.infos)
        summary = f"{bar}\n{'  '.join(parts)}"
        style = "red" if report.errors else "yellow" if report.warnings else "blue"
        console.print(Panel(summary, style=style, expand=False))
    console.print()


# ── Compact vs verbose printers ──────────────────────────────────────


def _print_entrypoints_compact(entrypoints: list[EntryPoint], project_root: Path) -> None:
    """Group entrypoints by file, show one line per file."""
    by_file: dict[Path, list[EntryPoint]] = defaultdict(list)
    for ep in entrypoints:
        by_file[ep.filepath].append(ep)

    for filepath, eps in by_file.items():
        rel = filepath.relative_to(project_root)
        calls = ", ".join(ep.description for ep in eps)
        console.print(f"    [green]\u2713[/green] [bold]{str(rel):<30s}[/bold] {calls}")


def _print_entrypoints_verbose(entrypoints: list[EntryPoint], project_root: Path) -> None:
    """Show every entrypoint on its own line with line numbers."""
    for ep in entrypoints:
        rel = ep.filepath.relative_to(project_root)
        loc = f"{rel}:{ep.lineno}"
        guard = " [dim]\u2190 if __name__[/dim]" if ep.in_main_guard else ""
        console.print(f"    [green]\u2713[/green] [bold]{loc:<30s}[/bold] {ep.description}{guard}")


def _print_orphans_compact(orphans: list, project_root: Path, sev: Severity, report: Report) -> None:
    """Group orphans by reason, show counts + top files."""
    by_reason: dict[str, list[str]] = defaultdict(list)
    for orphan in orphans:
        rel = str(orphan.filepath.relative_to(project_root))
        by_reason[orphan.reason].append(rel)
        report.add("orphans", rel, orphan.reason, sev, rel)

    for reason, files in by_reason.items():
        style, icon = SEVERITY_STYLE[sev]
        console.print(f"    [{style}]{icon}[/{style}] [bold]{len(files)}[/bold] files \u2192 [yellow]{reason}[/yellow]")
        for f in files[:3]:
            console.print(f"      [dim]{f}[/dim]")
        if len(files) > 3:
            console.print(f"      [dim]... and {len(files) - 3} more[/dim]")


def _print_orphans_verbose(orphans: list, project_root: Path, sev: Severity, report: Report) -> None:
    """Show every orphan on its own line."""
    for orphan in orphans:
        orphan_rel = str(orphan.filepath.relative_to(project_root))
        report.add("orphans", orphan_rel, orphan.reason, sev, orphan_rel)
        _finding_line(orphan_rel, f"[dim]never imported \u2192[/dim] [yellow]{orphan.reason}[/yellow]", sev)


def _print_sideeffects_compact(
    sideeffects: list[SideEffect], project_root: Path, sev: Severity, report: Report
) -> None:
    """Group side effects by file, show count per file."""
    by_file: dict[str, list[str]] = defaultdict(list)
    for se in sideeffects:
        rel = str(se.filepath.relative_to(project_root))
        by_file[rel].append(se.call_text)
        report.add("side_effects", se.call_text, "", sev, f"{rel}:{se.lineno}")

    for filepath, calls in by_file.items():
        style, icon = SEVERITY_STYLE[sev]
        call_summary = ", ".join(calls[:3])
        extra = f" +{len(calls) - 3} more" if len(calls) > 3 else ""
        console.print(
            f"    [{style}]{icon}[/{style}] [bold]{filepath:<30s}[/bold] [yellow]{call_summary}{extra}[/yellow]"
        )


def _print_sideeffects_verbose(
    sideeffects: list[SideEffect], project_root: Path, sev: Severity, report: Report
) -> None:
    """Show every side effect on its own line."""
    for se in sideeffects:
        rel = se.filepath.relative_to(project_root)
        loc = f"{rel}:{se.lineno}"
        report.add("side_effects", se.call_text, "", sev, loc)
        _finding_line(loc, f"[yellow]{se.call_text}[/yellow]", sev)


# ── Main command ─────────────────────────────────────────────────────


def audit_cmd(
    path: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    verbose: bool = False,
    format: str = "rich",
) -> None:
    """Run the full audit."""
    project_root = Path(path)
    project_name = project_root.name
    spec = load_spec()
    config = load_project_config(project_root)
    report = Report()

    inc = include or config.include or None
    exc = exclude or config.exclude or None

    # Gather all data
    stack = detect_stack(project_root, spec)
    entrypoints = detect_entrypoints(project_root, inc, exc)
    graph = build_import_graph(project_root, inc, exc)
    orphans = detect_orphans(project_root, graph)
    if config.ignore_orphans:
        orphans = [
            o
            for o in orphans
            if not any(fnmatch.fnmatch(str(o.filepath.relative_to(project_root)), pat) for pat in config.ignore_orphans)
        ]
    sideeffects = detect_sideeffects(project_root, spec, inc, exc)
    if config.ignore_side_effects:
        sideeffects = [
            se
            for se in sideeffects
            if not any(fnmatch.fnmatch(se.call_text, pat) for pat in config.ignore_side_effects)
        ]
    configs = detect_config_files(project_root, spec)
    deps = detect_deps(project_root)
    unused = detect_unused_deps(project_root, deps, spec, inc, exc)
    if config.ignore_deps:
        unused = [d for d in unused if d not in config.ignore_deps]
    stats = graph_summary(graph, len(orphans))

    # Populate report for orphans
    sev = config.severity_orphans
    for orphan in orphans:
        orphan_rel = str(orphan.filepath.relative_to(project_root))
        report.add("orphans", orphan_rel, orphan.reason, sev, orphan_rel)

    # Populate report for side effects
    sev = config.severity_side_effects
    for se in sideeffects:
        rel = str(se.filepath.relative_to(project_root))
        report.add("side_effects", se.call_text, "", sev, f"{rel}:{se.lineno}")

    # Populate report for unused deps
    sev = config.severity_unused_deps
    for dep in unused:
        report.add("unused_deps", dep, "in pyproject.toml, 0 imports found", sev, dep)

    if format == "json":
        _print_json(project_root, stack, entrypoints, orphans, sideeffects, configs, unused, stats, report)
        sys.exit(report.exit_code)
        return

    # Rich output
    console.print()
    console.print(Panel(Text(f"  ca audit \u2014 {project_name}/", style="bold"), style="dim", expand=False))

    # Stack
    _section_header(f"STACK \u2014 {project_name}/", None, None)
    if stack:
        order = list(spec.categories.keys())
        for cat in order:
            if cat not in stack:
                continue
            label = spec.categories.get(cat, cat)
            pkgs = ", ".join(stack[cat])
            console.print(f"    [bold]{label + ':':<20s}[/bold] [cyan]{pkgs}[/cyan]")
        for cat in sorted(stack):
            if cat not in spec.categories:
                pkgs = ", ".join(stack[cat])
                console.print(f"    [bold]{cat + ':':<20s}[/bold] [cyan]{pkgs}[/cyan]")
    else:
        console.print("    [dim](no recognized dependencies)[/dim]")

    # Entry points
    _section_header(f"ENTRY POINTS ({len(entrypoints)})", None, None)
    if entrypoints:
        if verbose:
            _print_entrypoints_verbose(entrypoints, project_root)
        else:
            _print_entrypoints_compact(entrypoints, project_root)
    else:
        console.print("    [dim](none found)[/dim]")

    # Orphans
    sev = config.severity_orphans
    _section_header(f"ORPHAN FILES ({len(orphans)})", len(orphans), sev)
    if orphans:
        if verbose:
            _print_orphans_verbose(orphans, project_root, sev, Report())
        else:
            _print_orphans_compact(orphans, project_root, sev, Report())
    else:
        console.print("    [green]All files are reachable via imports[/green]")

    # Side effects
    sev = config.severity_side_effects
    _section_header(f"SIDE EFFECTS ON IMPORT ({len(sideeffects)})", len(sideeffects), sev)
    if sideeffects:
        if verbose:
            _print_sideeffects_verbose(sideeffects, project_root, sev, Report())
        else:
            _print_sideeffects_compact(sideeffects, project_root, sev, Report())
    else:
        console.print("    [green]No module-level side effects detected[/green]")

    # Config files
    _section_header(f"CONFIG FILES ({len(configs)})", None, None)
    if configs:
        for cfg in configs:
            rel = cfg.path.relative_to(project_root)
            display = str(rel) + ("/" if cfg.path.is_dir() else "")
            console.print(f"    [blue]\u2022[/blue] [bold]{display:<30s}[/bold] [dim]{cfg.description}[/dim]")
    else:
        console.print("    [dim](none found)[/dim]")

    # Unused deps
    sev = config.severity_unused_deps
    _section_header(f"UNUSED DEPS ({len(unused)})", len(unused), sev)
    if unused:
        for dep in unused:
            _finding_line(dep, "[dim]in pyproject.toml, 0 imports found[/dim]", sev)
    else:
        console.print("    [green]All declared deps are imported[/green]")

    # Import graph
    _section_header("IMPORT GRAPH", None, None)
    connected = stats["connected"]
    total = stats["total"]
    orphan_count = stats["orphans"]
    if total > 0:
        bar = _health_bar(connected, orphan_count, total, 50)
        console.print(f"    {bar}")
        console.print(
            f"    [green]{connected} connected[/green]  [red]{orphan_count} orphans[/red]  [dim]{total} total[/dim]"
        )
    if stats["longest_chain"] > 1:
        console.print(f"    [dim]longest import chain:[/dim] [cyan]{stats['longest_chain']} deep[/cyan]")

    _print_summary(report)
    sys.exit(report.exit_code)


def _print_json(
    project_root: Path,
    stack: dict,
    entrypoints: list[EntryPoint],
    orphans: list,
    sideeffects: list[SideEffect],
    configs: list,
    unused: list[str],
    graph_stats: dict,
    report: Report,
) -> None:
    """Print audit results as JSON."""
    data = {
        "stack": {cat: list(pkgs) for cat, pkgs in stack.items()},
        "entrypoints": [
            {
                "file": str(ep.filepath.relative_to(project_root)),
                "lineno": ep.lineno,
                "description": ep.description,
                "in_main_guard": ep.in_main_guard,
            }
            for ep in entrypoints
        ],
        "orphans": [
            {
                "file": str(o.filepath.relative_to(project_root)),
                "reason": o.reason,
            }
            for o in orphans
        ],
        "sideeffects": [
            {
                "file": str(se.filepath.relative_to(project_root)),
                "lineno": se.lineno,
                "call_text": se.call_text,
            }
            for se in sideeffects
        ],
        "config_files": [
            {
                "path": str(cfg.path.relative_to(project_root)),
                "description": cfg.description,
            }
            for cfg in configs
        ],
        "unused_deps": unused,
        "graph_summary": graph_stats,
        "report": {
            "errors": report.errors,
            "warnings": report.warnings,
            "infos": report.infos,
        },
    }
    print(json.dumps(data, indent=2))
