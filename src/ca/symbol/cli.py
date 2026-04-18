"""Root CLI — dispatches to subcommands (audit, loc, map, update-linguist)."""

import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .commands.analyze import analyze_cmd, analyze_dump
from .commands.audit import audit_cmd
from .commands.callers import callers_cmd
from .commands.code import code_cmd
from .commands.index import index_cmd
from .commands.init import init_cmd
from .commands.search import search_cmd
from .commands.loc import loc_cmd
from .shared.linguist.config.load import update_from_github
from .commands.map import map_cmd
from .commands.delete_symbol import delete_symbol_cmd
from .commands.hook import run as hook_run
from .commands.insert_symbol import insert_symbol_cmd
from .commands.outline import outline_cmd
from .commands.patch import patch_cmd
from .commands.rename_symbol import rename_symbol_cmd
from .commands.replace_symbol import replace_symbol_cmd

console = Console()

app = typer.Typer(help="symbol — codebase audit toolkit for Python projects.", no_args_is_help=True)

# Shared state
state = {"verbose": False}


def _maybe_default_audit() -> None:
    """If the first arg looks like a path (not a subcommand), inject 'audit'."""
    known = {
        "analyze",
        "audit",
        "callers",
        "code",
        "dump",
        "index",
        "init",
        "loc",
        "map",
        "mcp",
        "delete-symbol",
        "hook",
        "insert-symbol",
        "outline",
        "patch",
        "rename-symbol",
        "replace-symbol",
        "search",
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
    """symbol — codebase audit toolkit for Python projects."""
    if format not in ("rich", "json"):
        raise typer.BadParameter(f"Invalid format '{format}'. Choose from: rich, json")
    state["verbose"] = verbose
    state["format"] = format


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
    output: Annotated[str, typer.Option("-o", "--output", help="Output JSON file path")] = "ca-analysis.json",
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


@app.command()
def loc(
    path: Annotated[str, typer.Argument(help="Path to the project directory")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
) -> None:
    """Count lines of code by language — powered by GitHub Linguist."""
    loc_cmd(path, format=format)


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


@app.command()
def search(
    patterns: Annotated[list[str], typer.Argument(help="One or more symbol patterns. Multiple patterns = AND (all must match).")],
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    kind: Annotated[str | None, typer.Option("--kind", help="Filter by kind: class, function, async_function")] = None,
    file: Annotated[str | None, typer.Option("--file", help="Restrict to one file (repo-relative path)")] = None,
    regex: Annotated[bool, typer.Option("--regex", "-E", help="Treat patterns as Python regex (unanchored)")] = False,
    fixed: Annotated[bool, typer.Option("--fixed", "-F", help="Treat patterns as literal substrings")] = False,
    ignore_case: Annotated[bool, typer.Option("--ignore-case", "-i", help="Case-insensitive matching")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Max candidates to return")] = 100,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
) -> None:
    """Narrow candidates by name. Returns signatures, no bodies."""
    search_cmd(
        patterns,
        path=path,
        kind=kind,
        file=file,
        regex=regex,
        fixed=fixed,
        ignore_case=ignore_case,
        limit=limit,
        format=format,
    )


@app.command()
def code(
    target: Annotated[str, typer.Argument(help="file:start-end or fully qualified symbol path")],
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    no_refs: Annotated[bool, typer.Option("--no-refs", help="Skip reference list in output")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
) -> None:
    """Retrieve exact body at a known address. Use after symbol search."""
    code_cmd(target, path=path, include_refs=not no_refs, format=format)


@app.command()
def index(
    path: Annotated[str, typer.Argument(help="Project root")] = ".",
) -> None:
    """Build the symbol lookup table and write to .ca/symbol_index.pkl."""
    index_cmd(path)


@app.command()
def outline(
    file: Annotated[str, typer.Argument(help="File to outline")],
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
) -> None:
    """Show a file's symbols as a parent-child tree."""
    outline_cmd(file, path=path, format=format)


@app.command()
def callers(
    name: Annotated[str, typer.Argument(help="Name to search for (last segment matched)")],
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich or json")] = "rich",
) -> None:
    """Tier-1 textual reference scan — name-match only, unresolved."""
    callers_cmd(name, path=path, format=format)


@app.command()
def patch(
    file: Annotated[str, typer.Argument(help="File to patch")],
    range_: Annotated[str, typer.Option("--range", "-r", help="Line range A-B (inclusive, 1-indexed)")],
    content: Annotated[str | None, typer.Option("--content", help="New content (use '' or omit for delete)")] = None,
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    force: Annotated[bool, typer.Option("--force", help="Skip read-cache check")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Compute diff but don't write")] = False,
    context: Annotated[int, typer.Option("--context", "-C", help="Lines of diff context around the edit")] = 5,
    agent: Annotated[bool, typer.Option("--agent", help="Enriched plain-text output for LLM consumers")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich, agent, or json")] = "rich",
) -> None:
    """Byte-range edit primitive. Replace, delete (empty content), or insert (zero-width range)."""
    import os as _os
    if _os.environ.get("CA_AGENT"):
        agent = True
    patch_cmd(
        file=file,
        range=range_,
        content=content,
        project_root=path,
        force=force,
        dry_run=dry_run,
        context=context,
        agent=agent,
        format=format,
    )


@app.command("delete-symbol")
def delete_symbol(
    qualified_path: Annotated[str, typer.Argument(help="Fully qualified symbol path (e.g. services.user.UserService)")],
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    force: Annotated[bool, typer.Option("--force", help="Delete even if callers exist")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Compute diff but don't write")] = False,
    context: Annotated[int, typer.Option("--context", "-C", help="Lines of diff context")] = 5,
    agent: Annotated[bool, typer.Option("--agent", help="Enriched plain-text output for LLM consumers")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich, agent, or json")] = "rich",
) -> None:
    """Remove a named symbol from its file. Refuses if callers exist (use --force)."""
    import os as _os
    if _os.environ.get("CA_AGENT"):
        agent = True
    delete_symbol_cmd(
        qualified_path=qualified_path,
        project_root=path,
        force=force,
        dry_run=dry_run,
        context=context,
        agent=agent,
        format=format,
    )


@app.command("insert-symbol")
def insert_symbol(
    anchor: Annotated[str, typer.Argument(help="Anchor symbol qualified path")],
    position: Annotated[str, typer.Option("--position", help="before | after | start | end")],
    content: Annotated[str | None, typer.Option("--content", help="New content")] = None,
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    no_reindent: Annotated[bool, typer.Option("--no-reindent", help="Send content as-is, don't auto-indent")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Compute diff but don't write")] = False,
    context: Annotated[int, typer.Option("--context", "-C", help="Lines of diff context")] = 5,
    agent: Annotated[bool, typer.Option("--agent", help="Enriched plain-text output for LLM consumers")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich, agent, or json")] = "rich",
) -> None:
    """Insert code at a position anchored to a symbol (before/after/start/end)."""
    import os as _os
    if _os.environ.get("CA_AGENT"):
        agent = True
    insert_symbol_cmd(
        anchor=anchor,
        position=position,
        content=content,
        project_root=path,
        reindent=not no_reindent,
        dry_run=dry_run,
        context=context,
        agent=agent,
        format=format,
    )


@app.command("rename-symbol")
def rename_symbol(
    qualified_path: Annotated[str, typer.Argument(help="Fully qualified symbol path (e.g. services.user.UserService)")],
    new_name: Annotated[str, typer.Argument(help="New leaf name (no dots)")],
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan rename, don't write")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty", help="Proceed even with uncommitted changes")] = False,
    force_no_vcs: Annotated[bool, typer.Option("--force-no-vcs", help="Proceed on a non-git project")] = False,
    agent: Annotated[bool, typer.Option("--agent", help="Enriched plain-text output for LLM consumers")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich, agent, or json")] = "rich",
) -> None:
    """Rename a symbol (tier-1 textual) and update references across the project."""
    import os as _os
    if _os.environ.get("CA_AGENT"):
        agent = True
    rename_symbol_cmd(
        qualified_path=qualified_path,
        new_name=new_name,
        project_root=path,
        dry_run=dry_run,
        allow_dirty=allow_dirty,
        force_no_vcs=force_no_vcs,
        agent=agent,
        format=format,
    )


@app.command("replace-symbol")
def replace_symbol(
    qualified_path: Annotated[str, typer.Argument(help="Fully qualified symbol path to replace")],
    content: Annotated[str, typer.Option("--content", help="Full new symbol definition")],
    path: Annotated[str, typer.Option("--path", "-p", help="Project root")] = ".",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty", help="Proceed with uncommitted changes")] = False,
    force_no_vcs: Annotated[bool, typer.Option("--force-no-vcs", help="Proceed on a non-git project")] = False,
    agent: Annotated[bool, typer.Option("--agent", help="Enriched plain-text output for LLM consumers")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: rich, agent, or json")] = "rich",
) -> None:
    """Replace a symbol's full definition. If content declares a new name, callers are updated too."""
    import os as _os
    if _os.environ.get("CA_AGENT"):
        agent = True
    replace_symbol_cmd(
        qualified_path=qualified_path,
        content=content,
        project_root=path,
        dry_run=dry_run,
        allow_dirty=allow_dirty,
        force_no_vcs=force_no_vcs,
        agent=agent,
        format=format,
    )


@app.command(hidden=True)
def hook(
    enforce: Annotated[bool, typer.Option("--enforce", help="Block tool calls (exit 2) instead of soft-nudging via additionalContext.")] = False,
) -> None:
    """Hook entry — reads Claude Code hook JSON from stdin. Default: soft nudge. --enforce: hard block."""
    hook_run(enforce=enforce)


@app.command()
def mcp(
    root: Annotated[str, typer.Option("--root", "-r", help="Project root to serve")] = ".",
) -> None:
    """Run the MCP server (stdio) exposing symbol as agent tools."""
    from ca.symbol.mcp.server import serve
    serve(root)


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
