"""Root CLI — the Python commands (audit, analyze, dump, init, map,
update-linguist), plus help entries for the commands the binary runs."""

import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from click.core import ParameterSource
from rich.console import Console

from .commands.analyze import analyze_cmd, analyze_dump
from .commands.audit import audit_cmd
from .commands.init import init_cmd
from .commands.map import map_cmd
from .entrypoint import DELEGATED_COMMANDS, run_delegated
from .shared.linguist.config.load import update_from_github

console = Console()

app = typer.Typer(help="symbol — AST-native code intelligence for Python and Go.", no_args_is_help=True)

# Shared state
state = {"verbose": False}


def _maybe_default_audit() -> None:
    """If the first arg looks like a path (not a subcommand), inject 'audit'."""
    known = {
        "analyze",
        "audit",
        "dump",
        "init",
        "map",
        "update-linguist",
        *DELEGATED_COMMANDS,
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


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version

        console.print(version("wyolet-symbol"))
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show full detail")] = False,
    format: Annotated[str, typer.Option("--format", help="Output format: rich or json")] = "rich",
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed symbol version and exit",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """symbol — AST-native code intelligence for Python and Go."""
    if format not in ("rich", "json"):
        raise typer.BadParameter(f"Invalid format '{format}'. Choose from: rich, json")
    state["verbose"] = verbose
    state["format"] = format
    if ctx.get_parameter_source("format") == ParameterSource.COMMANDLINE and ctx.invoked_subcommand:
        # Subcommands own their --format; hand the global value down as their
        # default so an explicit subcommand --format still wins.
        sub = ctx.command.get_command(ctx, ctx.invoked_subcommand)
        if sub is None or not any(p.name == "format" for p in sub.params):
            raise typer.BadParameter(
                f"'{ctx.invoked_subcommand}' has no --format option", param_hint="--format"
            )
        ctx.default_map = {ctx.invoked_subcommand: {"format": format}}


@app.command()
def analyze(
    file: Annotated[str, typer.Argument(help="Path to the file to analyze")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
) -> None:
    """Analyze a single file — exports, imports, per-name blast radius."""
    file_path = Path(file).resolve()
    # Walk up to find the project root (directory with pyproject.toml or .git)
    project_root = file_path.parent
    while project_root != project_root.parent:
        if (project_root / "pyproject.toml").exists() or (project_root / ".git").exists():
            break
        project_root = project_root.parent
    target = str(file_path.relative_to(project_root))
    analyze_cmd(str(project_root), target, format=format)


@app.command()
def dump(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
    output: Annotated[str, typer.Option("-o", "--output", help="Output JSON file path")] = "symbol-analysis.json",
) -> None:
    """Dump per-file analysis of all Python files to JSON."""
    analyze_dump(path, output)


@app.command()
def audit(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
    include: Annotated[list[str] | None, typer.Option("-i", "--include", help="Glob patterns to include")] = None,
    exclude: Annotated[list[str] | None, typer.Option("-e", "--exclude", help="Glob patterns to exclude")] = None,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
) -> None:
    """Audit a Python codebase — detect stack, entry points, orphans, side effects."""
    audit_cmd(
        path, include=include or [], exclude=exclude or [], verbose=state["verbose"], format=format
    )


@app.command()
def init(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
) -> None:
    """Analyze a project and generate a recommended [tool.symbol] config."""
    init_cmd(path)


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
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
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
        format=format,
    )


@app.command("update-linguist")
def update_linguist() -> None:
    """Update linguist language definitions from GitHub."""
    console.print()
    console.print("  Updating linguist configs from GitHub...")
    console.print()

    def on_progress(filename: str, status: str) -> None:
        if status == "ok":
            console.print(f"    [green]✓[/green] {filename}")
        else:
            console.print(f"    [red]✗[/red] {filename} [dim]({status})[/dim]")

    update_from_github(callback=on_progress)
    console.print()
    console.print("  [bold green]Done.[/bold green] Language definitions are up to date.")
    console.print()


def _run_on_binary() -> None:
    raise typer.Exit(run_delegated(sys.argv))


# The console entry hands these to the binary before Typer parses; the
# entries here list them in `symbol --help` and cover argv it did not route.
for _name, _help in DELEGATED_COMMANDS.items():
    app.command(
        _name,
        help=_help,
        hidden=_name == "hook",
        add_help_option=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )(_run_on_binary)


# Inject default subcommand before Typer parses
_maybe_default_audit()
