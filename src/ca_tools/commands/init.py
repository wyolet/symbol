"""Init command — analyze a project and generate a recommended [tool.ca-tools] config."""

import fnmatch
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

import ca_tools.checkers  # noqa: F401
from ca_tools.checkers.orphans import OrphanFile
from ca_tools.checkers.side_effects import SideEffect
from ca_tools.shared.context import build_context
from ca_tools.shared.findings import Report
from ca_tools.shared.runner import run_checkers

console = Console()

# Directories whose files are commonly false-positive orphans.
_ORPHAN_DIR_PATTERNS: list[tuple[str, str]] = [
    ("alembic/*", "alembic"),
    ("migrations/*", "migrations"),
    ("scripts/*", "scripts"),
    ("tools/*", "tools"),
    ("bin/*", "bin"),
    ("deploy/*", "deploy"),
    ("docs/*", "docs"),
]

# Side-effect call patterns that are typically expected / intentional.
_SAFE_SIDE_EFFECT_PATTERNS: list[str] = [
    "*.include_router()",
    "*.add_middleware()",
    "*.add_route()",
    "*.add_event_handler()",
    "*.register_blueprint()",
    "*.add_url_rule()",
    "*.register()",
]

# Deps that are commonly runtime-only (no direct import in source).
_RUNTIME_ONLY_DEPS: set[str] = {
    "greenlet",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
    "mysqlclient",
    "uvloop",
    "httptools",
    "watchfiles",
    "python-dotenv",
    "gunicorn",
    "gevent",
}


def _match_orphan_patterns(orphans: list[OrphanFile], project_root: Path) -> list[str]:
    """Find directory-level glob patterns that cover detected orphans."""
    matched_patterns: list[str] = []
    for pattern, _label in _ORPHAN_DIR_PATTERNS:
        hits = [o for o in orphans if fnmatch.fnmatch(str(o.filepath.relative_to(project_root)), pattern)]
        if hits:
            matched_patterns.append(pattern)
    return matched_patterns


def _match_side_effect_patterns(sideeffects: list[SideEffect]) -> list[str]:
    """Find known side-effect call patterns present in the results."""
    matched: list[str] = []
    for pattern in _SAFE_SIDE_EFFECT_PATTERNS:
        hits = [se for se in sideeffects if fnmatch.fnmatch(se.call_text, pattern)]
        if hits:
            matched.append(pattern)
    return matched


def _match_runtime_deps(unused: list[str]) -> list[str]:
    """Find unused deps that are known runtime-only packages."""
    return [d for d in unused if d in _RUNTIME_ONLY_DEPS]


def _generate_toml(
    exclude: list[str],
    ignore_orphans: list[str],
    ignore_side_effects: list[str],
    ignore_deps: list[str],
) -> str:
    """Build a TOML config string from the discovered patterns."""
    lines: list[str] = []
    lines.append("[tool.ca-tools]")

    if exclude:
        items = ", ".join(f'"{e}"' for e in exclude)
        lines.append(f"exclude = [{items}]")

    lines.append("")
    lines.append("[tool.ca-tools.checkers.orphans]")
    lines.append('severity = "warning"')
    if ignore_orphans:
        items = ", ".join(f'"{p}"' for p in ignore_orphans)
        lines.append(f"ignore = [{items}]")

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
    ignore_orphans = _match_orphan_patterns(orphans, project_root)
    ignore_side_effects = _match_side_effect_patterns(sideeffects)
    ignore_deps = _match_runtime_deps(unused)

    # Use orphan dir patterns as exclude candidates too
    exclude = list(ignore_orphans)

    toml_str = _generate_toml(
        exclude=exclude,
        ignore_orphans=ignore_orphans,
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

    if ignore_orphans:
        console.print(f"  [bold]Auto-ignored orphan patterns:[/bold] {', '.join(ignore_orphans)}")
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
