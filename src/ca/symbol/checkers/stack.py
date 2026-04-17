"""Stack detection — identify tech stack from declared dependencies."""

from rich.console import Console
from rich.text import Text

from ca.symbol.shared.context import AnalysisContext
from ca.symbol.shared.pkg_registry import lookup
from ca.symbol.shared.registry import register, views

I1, I2 = "  ", "    "


@register(
    name="stack",
    description="tech stack from declared dependencies",
    kind="project",
    contributes_to_report=False,
    priority=10,
)
def detect(ctx: AnalysisContext) -> dict[str, list[str]]:
    """Returns {category: [package_names]}."""
    stack: dict[str, list[str]] = {}
    for dep in ctx.deps:
        category = lookup(dep, ctx.spec)
        if category is not None:
            stack.setdefault(category, []).append(dep)
    return stack


# ── Views ────────────────────────────────────────────────────────────


def _collapse_packages(packages: list[str]) -> list[str]:
    bases: dict[str, str] = {}
    for pkg in packages:
        base = pkg.split("-")[0]
        if base not in bases or len(pkg) < len(bases[base]):
            bases[base] = pkg
    return list(bases.values())


def rich_view(stack: dict[str, list[str]], ctx: AnalysisContext, console: Console) -> None:
    console.print()
    console.print(Text(f"{I1}\U0001f4e6 STACK", style="bold"))
    console.print()

    if not stack:
        console.print(f"{I2}[dim](no recognized dependencies)[/dim]")
        return

    spec = ctx.spec
    if ctx.verbose:
        order = list(spec.categories.keys())
        for cat in order:
            if cat not in stack:
                continue
            label = spec.categories.get(cat, cat)
            pkgs = ", ".join(stack[cat])
            console.print(f"{I2}[bold]{label + ':':<20s}[/bold] [cyan]{pkgs}[/cyan]")
        for cat in sorted(stack):
            if cat not in spec.categories:
                pkgs = ", ".join(stack[cat])
                console.print(f"{I2}[bold]{cat + ':':<20s}[/bold] [cyan]{pkgs}[/cyan]")
    else:
        order = list(spec.categories.keys())
        shown = []
        others: list[str] = []

        for cat in order:
            if cat not in stack:
                continue
            label = spec.categories.get(cat, cat)
            pkgs = _collapse_packages(stack[cat])
            if cat in ctx.spec.stack.primary_categories:
                shown.append((label, pkgs))
            else:
                others.extend(pkgs)

        for cat in sorted(stack):
            if cat not in spec.categories:
                others.extend(_collapse_packages(stack[cat]))

        for label, pkgs in shown:
            console.print(f"{I2}[bold]{label + ':':<20s}[/bold] [cyan]{', '.join(pkgs)}[/cyan]")

        if others:
            console.print(f"{I2}[dim]also:[/dim] [dim]{', '.join(sorted(set(others)))}[/dim]")


def json_view(stack: dict[str, list[str]], ctx: AnalysisContext) -> dict:
    return {cat: list(pkgs) for cat, pkgs in stack.items()}


views("stack", rich=rich_view, json=json_view)
