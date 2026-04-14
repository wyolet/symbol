"""Orphan file detection — find Python files unreachable from any entry point."""

import ast
import fnmatch
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ca_tools.shared.context import AnalysisContext
from ca_tools.shared.findings import Finding, Severity
from ca_tools.shared.import_graph import build_import_graph
from ca_tools.shared.registry import register, views

I1, I2 = "  ", "    "


@dataclass
class OrphanFile:
    filepath: Path
    reason: str
    severity: Severity = Severity.WARNING


@register(
    name="orphans",
    description="Python files unreachable from any entry point",
    kind="project",
    contributes_to_report=True,
    priority=40,
)
def detect(ctx: AnalysisContext) -> list[OrphanFile]:
    graph = build_import_graph(
        ctx.project_root,
        cache=ctx.cache,
        propagate_init=True,
    )

    imported: set[Path] = set()
    for targets in graph.resolved_edges.values():
        imported.update(targets)

    # Entry point files — skip from orphan detection
    entry_point_files: set[Path] = set()
    for filepath in ctx.cache.files:
        tree = ctx.cache.get_ast(filepath)
        if tree and _has_main_guard(tree):
            entry_point_files.add(filepath)

    skip_patterns = list(ctx.spec.orphan.skip_patterns) + list(ctx.resolved.skip_orphan_patterns)

    orphans: list[OrphanFile] = []
    for py_file in graph.files:
        if py_file in imported:
            continue
        if py_file in entry_point_files:
            continue

        rel = py_file.relative_to(ctx.project_root)
        rel_str = str(rel)
        name = rel.name

        if _matches_skip(name, rel_str, skip_patterns):
            continue

        if "script" in rel_str or "scripts" in rel_str:
            reason = "likely one-off script"
        elif "test" in rel_str or "tests" in rel_str:
            reason = "likely test file"
        else:
            reason = "likely dead code"

        orphans.append(OrphanFile(filepath=py_file, reason=reason))

    return orphans


# ── Views ─────────────────────────────────────────────────────────────


def to_findings(items: list[OrphanFile], ctx: AnalysisContext) -> list[Finding]:
    findings = []
    for orphan in items:
        rel = str(orphan.filepath.relative_to(ctx.project_root))
        findings.append(Finding(
            section="orphans",
            message=rel,
            detail=orphan.reason,
            severity=ctx.resolved.severity_overrides.get("orphans", Severity.ERROR),
            location=rel,
        ))
    return findings


def rich_view(items: list[OrphanFile], ctx: AnalysisContext, console: Console) -> None:
    console.print()
    console.print(Text(f"{I1}\U0001f47b ORPHAN FILES ({len(items)})", style="bold yellow"))
    console.print()

    if not items:
        console.print(f"{I2}[dim](none found — all files are reachable)[/dim]")
        return

    console.print(f"{I2}[dim]Files not imported by anything — may be dead code[/dim]")
    console.print()

    limit = None if ctx.verbose else 20
    shown = items[:limit] if limit else items

    for orphan in shown:
        rel = str(orphan.filepath.relative_to(ctx.project_root))
        console.print(f"{I2}[yellow]![/yellow] [bold]{rel}[/bold]  [dim]{orphan.reason}[/dim]")

    if limit and len(items) > limit:
        console.print(f"{I2}[dim]... and {len(items) - limit} more (use -v to see all)[/dim]")


def json_view(items: list[OrphanFile], ctx: AnalysisContext) -> list[dict]:
    return [
        {
            "file": str(o.filepath.relative_to(ctx.project_root)),
            "reason": o.reason,
        }
        for o in items
    ]


views("orphans", findings=to_findings, rich=rich_view, json=json_view)


# ── Helpers ────────────────────────────────────────────────────────────


def _has_main_guard(tree: ast.Module) -> bool:
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If) and _is_main_guard(node):
            return True
    return False


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    if len(test.comparators) != 1:
        return False
    left, right = test.left, test.comparators[0]
    return (
        (_is_name(left, "__name__") and _is_str(right, "__main__"))
        or (_is_str(left, "__main__") and _is_name(right, "__name__"))
    )


def _is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_str(node: ast.expr, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _matches_skip(name: str, rel_str: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if name == pattern:
            return True
        if fnmatch.fnmatch(name, pattern):
            return True
        if fnmatch.fnmatch(rel_str, pattern):
            return True
    return False
