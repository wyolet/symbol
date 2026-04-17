"""Audit CLI — detect stack, entry points, orphans, side effects."""

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

import ca.symbol.checkers  # noqa: F401 — registers all checkers
from ca.symbol.shared.linguist import Linguist
from ca.symbol.shared.context import AnalysisContext, build_context
from ca.symbol.shared.findings import Report
from ca.symbol.shared.registry import get, get_all
from ca.symbol.shared.runner import run_checkers

console = Console()

I1 = "  "
I2 = "    "


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
    report = Report()

    ctx = build_context(project_root, include, exclude, verbose)

    # Run all registered checkers
    results = run_checkers(ctx, report)

    # Save parse failures before clearing cache
    parse_failures = list(ctx.cache.failed)
    ctx.cache.clear()

    # Linguist for project shape (cheap, ~300ms)
    linguist = Linguist()
    loc_stats = linguist.detect_directory(str(project_root))

    if format == "json":
        _output_json(results, ctx, loc_stats, parse_failures, report)
        sys.exit(report.exit_code)
        return

    # ── Rich output ──────────────────────────────────────────────────
    console.print()
    console.print(Panel(Text(f"{I1}symbol audit \u2014 {project_name}/", style="bold"), style="dim", expand=False))

    if parse_failures:
        _print_parse_failures(parse_failures, project_root)

    # Project shape (LOC + code structure) — combined section
    cs_entry = get("code_structure")
    cs_items = results.get("code_structure", [])
    _print_shape(loc_stats, cs_items[0] if cs_items else None)

    # Render each checker.s rich view
    for entry in get_all():
        name = entry.info.name
        if name in ctx.resolved.disabled_checkers:
            continue
        items = results.get(name, [])
        if entry.rich_view is not None:
            entry.rich_view(items, ctx, console)

    _print_summary(report)
    sys.exit(report.exit_code)


# ── Shape printer (combines LOC + code_structure) ────────────────────


def _print_shape(loc_stats: list[dict], cs) -> None:
    console.print()

    code_langs = [s for s in loc_stats if s.get("type") == "programming"]
    code_langs.sort(key=lambda s: s["sloc"], reverse=True)
    total_sloc = sum(s["sloc"] for s in loc_stats)
    total_files = sum(s["files"] for s in loc_stats)
    total_size = sum(s["size"] for s in loc_stats)

    primary = code_langs[0] if code_langs else None
    primary_pct = f"{primary['percentage_lines']:.0f}%" if primary and primary.get("percentage_lines") else ""
    primary_name = primary["name"] if primary else "Unknown"
    primary_color = primary.get("color", "#888") if primary else "#888"

    console.print(Text(f"{I1}\U0001f4d0 PROJECT SHAPE", style="bold"))
    console.print()

    console.print(
        f"{I2}[bold]{total_sloc:,}[/bold] [dim]sloc[/dim]  "
        f"[bold]{total_files:,}[/bold] [dim]files[/dim]  "
        f"[bold]{total_size / 1024:.0f}K[/bold] [dim]size[/dim]  "
        f"[{primary_color}]\u25cf[/{primary_color}] [bold]{primary_name}[/bold] [dim]{primary_pct}[/dim]"
    )

    if cs is not None:
        console.print(
            f"{I2}[bold]{cs.functions}[/bold] [dim]functions[/dim]  "
            f"[bold]{cs.methods}[/bold] [dim]methods[/dim]  "
            f"[bold]{cs.classes}[/bold] [dim]classes[/dim]  "
            f"[bold]{cs.avg_function_lines}[/bold] [dim]avg lines/fn[/dim]"
        )


# ── Parse failures ───────────────────────────────────────────────────


def _print_parse_failures(failures: list[tuple[Path, str]], project_root: Path) -> None:
    console.print()
    console.print(Text(f"{I1}\u26a0\ufe0f  PARSE ERRORS ({len(failures)})", style="bold red"))
    console.print(f"{I2}[dim]These files could not be parsed — merge conflicts, syntax errors, or encoding issues[/dim]")
    console.print()
    for filepath, error in failures[:10]:
        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)
        console.print(f"{I2}[red]\u2717[/red] [bold]{rel}[/bold]  [dim]{error}[/dim]")
    if len(failures) > 10:
        console.print(f"{I2}[dim]... and {len(failures) - 10} more[/dim]")


# ── Summary ──────────────────────────────────────────────────────────


def _print_summary(report: Report) -> None:
    console.print()
    parts: list[str] = []
    if report.errors:
        parts.append(f"[bold red]{report.errors} error{'s' if report.errors != 1 else ''}[/bold red]")
    if report.warnings:
        parts.append(f"[bold yellow]{report.warnings} warning{'s' if report.warnings != 1 else ''}[/bold yellow]")

    if not parts:
        console.print(Panel("[bold green]\u2705 No issues found[/bold green]", style="green", expand=False))
    else:
        console.print(Panel("  ".join(parts), style="red" if report.errors else "yellow", expand=False))
    console.print()


# ── JSON output ──────────────────────────────────────────────────────


def _output_json(results: dict, ctx: AnalysisContext, loc_stats: list[dict], parse_failures: list, report: Report) -> None:
    data: dict = {}

    # Parse errors
    data["parse_errors"] = [
        {"file": str(f.relative_to(ctx.project_root)), "error": err}
        for f, err in parse_failures
    ]

    # LOC shape
    data["shape"] = {
        "sloc": sum(s["sloc"] for s in loc_stats),
        "loc": sum(s["loc"] for s in loc_stats),
        "files": sum(s["files"] for s in loc_stats),
        "size": sum(s["size"] for s in loc_stats),
    }

    # Each detector's json_view
    for entry in get_all():
        name = entry.info.name
        if name in ctx.resolved.disabled_checkers:
            continue
        items = results.get(name, [])
        if entry.json_view is not None:
            json_data = entry.json_view(items, ctx)
            # code_structure merges into shape
            if name == "code_structure":
                data["shape"].update(json_data)
            else:
                data[name] = json_data
        else:
            data[name] = items

    # Report summary
    data["report"] = {
        "errors": report.errors,
        "warnings": report.warnings,
    }

    print(json.dumps(data, indent=2))
