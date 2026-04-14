"""Unused dependency detection — find deps declared but never imported."""

import ast

from rich.console import Console

from ca_tools.shared.context import AnalysisContext
from ca_tools.shared.pkg_registry import normalize_package_name
from ca_tools.shared.registry import register, views
from ca_tools.shared.findings import Finding, Severity

I1, I2 = "  ", "    "


@register(
    name="unused_deps",
    description="dependencies declared but never imported",
    kind="project",
    default_severity=Severity.ERROR,
    priority=40,
)
def detect(ctx: AnalysisContext) -> list[str]:
    """Returns list of unused dependency names."""
    all_imports = _collect_all_imports(ctx)
    ignore_deps = ctx.resolved.ignore_patterns.get("unused_deps", [])
    unused: list[str] = []

    for dep in ctx.deps:
        if dep in ignore_deps:
            continue
        import_name = _dep_to_import_name(dep, ctx)
        if import_name not in all_imports:
            unused.append(dep)

    return unused


# ── Views ────────────────────────────────────────────────────────────


def to_findings(items: list[str], ctx: AnalysisContext) -> list[Finding]:
    sev = ctx.resolved.severity_overrides.get("unused_deps", Severity.ERROR)
    return [
        Finding(
            section="unused_deps",
            message=dep,
            detail="in pyproject.toml, 0 imports found",
            severity=sev,
            location=dep,
        )
        for dep in items
    ]


def rich_view(items: list[str], ctx: AnalysisContext, console: Console) -> None:
    console.print()
    if not ctx.deps:
        return

    parts = [f"[bold]{len(ctx.deps)}[/bold] [dim]deps declared[/dim]"]
    if items:
        parts.append(f"[red]{len(items)} unused[/red] [dim]\u2014 run[/dim] [bold]ca deps[/bold] [dim]for details[/dim]")
    else:
        parts.append("[green]all imported[/green]")
    console.print(f"{I1}{'  '.join(parts)}")


def json_view(items: list[str], ctx: AnalysisContext) -> dict:
    return {
        "declared": len(ctx.deps),
        "unused": items,
    }


views("unused_deps", rich=rich_view, json=json_view, findings=to_findings)


# ── Helpers ──────────────────────────────────────────────────────────


def _collect_all_imports(ctx: AnalysisContext) -> set[str]:
    all_imports: set[str] = set()
    for py_file in ctx.cache.files:
        tree = ctx.cache.get_ast(py_file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    all_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    all_imports.add(node.module.split(".")[0])
    return all_imports


def _dep_to_import_name(dep: str, ctx: AnalysisContext) -> str:
    normalized = normalize_package_name(dep)
    pkg_info = ctx.spec.packages.get(normalized)
    if pkg_info and pkg_info.import_name:
        return pkg_info.import_name.split(".")[0]
    return normalized.replace("-", "_")
