"""Root CLI — dispatches to subcommands (audit, loc, map, update-linguist)."""

import os
import sys
from typing import Annotated

import typer
from rich.console import Console

from .audit.cli import audit_cmd
from .init.cli import init_cmd
from .loc.cli import loc_cmd
from .loc.linguist.config.load import update_from_github
from .map.cli import map_cmd

console = Console()

app = typer.Typer(help="ca-tools — codebase audit toolkit for Python projects.", no_args_is_help=True)

# Shared state
state = {"verbose": False}


def _maybe_default_audit() -> None:
    """If the first arg looks like a path (not a subcommand), inject 'audit'."""
    known = {
        "audit",
        "init",
        "loc",
        "map",
        "update-linguist",
        "--help",
        "-h",
        "--verbose",
        "-v",
        "--format",
        "--install-completion",
        "--show-completion",
    }
    args = sys.argv[1:]
    if args and args[0] not in known and not args[0].startswith("-"):
        # Check if it looks like a path
        candidate = args[0]
        if os.path.isdir(candidate) or "/" in candidate or "." in candidate:
            sys.argv.insert(1, "audit")


@app.callback(invoke_without_command=True)
def main(
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show full detail")] = False,
    format: Annotated[str, typer.Option("--format", help="Output format: rich or json")] = "rich",
) -> None:
    """ca-tools — codebase audit toolkit for Python projects."""
    if format not in ("rich", "json"):
        raise typer.BadParameter(f"Invalid format '{format}'. Choose from: rich, json")
    state["verbose"] = verbose
    state["format"] = format


@app.command()
def audit(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
    include: Annotated[list[str] | None, typer.Option("-i", "--include", help="Glob patterns to include")] = None,
    exclude: Annotated[list[str] | None, typer.Option("-e", "--exclude", help="Glob patterns to exclude")] = None,
) -> None:
    """Audit a Python codebase — detect stack, entry points, orphans, side effects."""
    audit_cmd(
        path, include=include or [], exclude=exclude or [], verbose=state["verbose"], format=state.get("format", "rich")
    )


@app.command()
def init(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
) -> None:
    """Analyze a project and generate a recommended [tool.ca-tools] config."""
    init_cmd(path)


@app.command()
def loc(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
) -> None:
    """Count lines of code by language — powered by GitHub Linguist."""
    loc_cmd(path, format=state.get("format", "rich"))


@app.command("map")
def map_command(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
    include: Annotated[list[str] | None, typer.Option("-i", "--include", help="Glob patterns to include")] = None,
    exclude: Annotated[list[str] | None, typer.Option("-e", "--exclude", help="Glob patterns to exclude")] = None,
    blast: Annotated[str | None, typer.Option(help="Show blast radius for a file")] = None,
    severity: Annotated[str | None, typer.Option("-s", "--severity", help="Minimum severity to show: info, warning, error")] = None,
    coupling_depth: Annotated[int, typer.Option("--coupling-depth", help="Package depth for coupling analysis (1=top-level, 2=sub-packages)")] = 1,
    show: Annotated[str | None, typer.Option(help="Show full detail for one section: cycles, hotspots, fragile, chains, leafs, coupling")] = None,
    limit: Annotated[int | None, typer.Option("-n", "--limit", help="Max items per section in summary view")] = None,
) -> None:
    """Map the import graph — find cycles, hotspots, and fragile modules."""
    map_cmd(
        path,
        include=include or [],
        exclude=exclude or [],
        blast=blast,
        severity=severity,
        coupling_depth=coupling_depth,
        show=show,
        limit=limit,
        format=state.get("format", "rich"),
    )


@app.command("update-linguist")
def update_linguist() -> None:
    """Update linguist language definitions from GitHub."""
    console.print()
    console.print("  Updating linguist configs from GitHub...")
    console.print()

    def on_progress(filename: str, status: str) -> None:
        if status == "ok":
            console.print(f"    [green]\u2713[/green] {filename}")
        else:
            console.print(f"    [red]\u2717[/red] {filename} [dim]({status})[/dim]")

    update_from_github(callback=on_progress)
    console.print()
    console.print("  [bold green]Done.[/bold green] Language definitions are up to date.")
    console.print()


# Inject default subcommand before Typer parses
_maybe_default_audit()
