"""Init command — analyze a project and generate a recommended [tool.ca-tools] config."""

import fnmatch
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

import ca_tools.checkers  # noqa: F401
from ca_tools.checkers.side_effects import SideEffect
from ca_tools.shared.context import build_context
from ca_tools.shared.findings import Report
from ca_tools.shared.runner import run_checkers
from ca_tools.shared.spec import Spec

console = Console()


def _match_side_effect_patterns(sideeffects: list[SideEffect], spec: Spec) -> list[str]:
    """Find known safe side-effect call patterns present in the results."""
    matched: list[str] = []
    for pattern in spec.init.safe_side_effect_patterns:
        hits = [se for se in sideeffects if fnmatch.fnmatch(se.call_text, pattern)]
        if hits:
            matched.append(pattern)
    return matched


def _match_runtime_deps(unused: list[str], spec: Spec) -> list[str]:
    """Find unused deps that are marked runtime_only in the spec."""
    runtime_only = {name for name, pkg in spec.packages.items() if pkg.runtime_only}
    return [d for d in unused if d in runtime_only]


def _generate_toml(
    ignore_side_effects: list[str],
    ignore_deps: list[str],
) -> str:
    """Build a TOML config string from the discovered patterns."""
    lines: list[str] = []
    lines.append("[tool.ca-tools]")

    lines.append("")
    lines.append("[tool.ca-tools.checkers.orphans]")
    lines.append('severity = "warning"')

    lines.append("")
    lines.append("[tool.ca-tools.checkers.side_effects]")
    lines.append('severity = "info"')
    if ignore_side_effects:
        items = ", ".join(f'"{p}"' for p in ignore_side_effects)
        lines.append(f"ignore = [{items}]")

    lines.append("")
    lines.append("[tool.ca-tools.checkers.unused_deps]")
    if ignore_deps:
        items = ", ".join(f'"{d}"' for d in ignore_deps)
        lines.append(f"ignore = [{items}]")

    return "\n".join(lines) + "\n"


def init_cmd(path: str) -> None:
    """Analyze a project and generate a recommended [tool.ca-tools] config."""
    project_root = Path(path).resolve()
    project_name = project_root.name

    console.print()
    console.print(
        Panel(
            Text(f"  ca init — analyzing {project_name}/", style="bold"),
            style="dim",
            expand=False,
        )
    )
    console.print()

    # Run analysis passes
    console.print("  [dim]Analyzing project...[/dim]")
    ctx = build_context(project_root)
    results = run_checkers(ctx, Report())

    orphans = results.get("orphans", [])
    sideeffects = results.get("side_effects", [])
    unused = results.get("unused_deps", [])

    # Match patterns
    ignore_side_effects = _match_side_effect_patterns(sideeffects, ctx.spec)
    ignore_deps = _match_runtime_deps(unused, ctx.spec)

    toml_str = _generate_toml(
        ignore_side_effects=ignore_side_effects,
        ignore_deps=ignore_deps,
    )

    # Print results summary
    console.print()
    console.print("  [bold]Found:[/bold]")
    console.print(f"    Orphans:        {len(orphans)}")
    console.print(f"    Side effects:   {len(sideeffects)}")
    console.print(f"    Unused deps:    {len(unused)}")
    console.print()

    if ignore_side_effects:
        console.print(f"  [bold]Auto-ignored side effect patterns:[/bold] {', '.join(ignore_side_effects)}")
    if ignore_deps:
        console.print(f"  [bold]Auto-ignored runtime deps:[/bold] {', '.join(ignore_deps)}")

    console.print()
    console.print(
        Panel(
            Text("Add this to your pyproject.toml:", style="bold"),
            style="green",
            expand=False,
        )
    )
    console.print()

    syntax = Syntax(toml_str, "toml", theme="monokai", padding=1)
    console.print(syntax)
