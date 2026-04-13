"""Side effect detection — find bare function calls at module level."""

import ast
import fnmatch
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ca_tools.shared.context import AnalysisContext
from ca_tools.shared.registry import register, views
from ca_tools.shared.findings import SEVERITY_STYLE, Finding, Severity

I1, I2 = "  ", "    "


@dataclass
class SideEffect:
    filepath: Path
    lineno: int
    call_text: str


@register(
    name="side_effects",
    description="bare function calls at module level — runs on import",
    kind="file",
    default_severity=Severity.WARNING,
    priority=50,
)
def detect(
    ctx: AnalysisContext,
    filepath: Path,
    tree: ast.Module | None,
) -> list[SideEffect]:
    if tree is None:
        return []

    safe_calls = ctx.resolved.safe_calls
    known_effects = ctx.resolved.known_effects
    ignore_patterns = ctx.resolved.ignore_patterns.get("side_effects", [])

    results: list[SideEffect] = []

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Expr):
            continue
        if not isinstance(node.value, ast.Call):
            continue

        call = node.value
        call_name = _get_call_name(call)
        if call_name is None:
            continue

        leaf_name = call_name.rsplit(".", 1)[-1]
        if leaf_name in safe_calls:
            continue

        if leaf_name.startswith("_") or leaf_name[0].isupper():
            if call_name not in known_effects:
                continue

        call_text = f"{call_name}()"

        if ignore_patterns and any(fnmatch.fnmatch(call_text, pat) for pat in ignore_patterns):
            continue

        results.append(SideEffect(filepath=filepath, lineno=node.lineno, call_text=call_text))

    return results


# ── Views ────────────────────────────────────────────────────────────


def to_findings(items: list[SideEffect], ctx: AnalysisContext) -> list[Finding]:
    sev = ctx.resolved.severity_overrides.get("side_effects", Severity.WARNING)
    return [
        Finding(
            section="side_effects",
            message=se.call_text,
            severity=sev,
            location=f"{se.filepath.relative_to(ctx.project_root)}:{se.lineno}",
        )
        for se in items
    ]


def rich_view(items: list[SideEffect], ctx: AnalysisContext, console: Console) -> None:
    sev = ctx.resolved.severity_overrides.get("side_effects", Severity.WARNING)

    console.print()
    if not items:
        console.print(Text(f"{I1}\u2705 SIDE EFFECTS (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No module-level side effects detected[/green]")
        return

    style_map = {Severity.ERROR: "bold red", Severity.WARNING: "bold yellow", Severity.INFO: "bold blue"}
    icon_map = {Severity.ERROR: "\U0001f534", Severity.WARNING: "\u26a0\ufe0f ", Severity.INFO: "\u2139\ufe0f "}
    console.print(Text(f"{I1}{icon_map[sev]} SIDE EFFECTS ({len(items)})", style=style_map[sev]))
    console.print(f"{I2}[dim]Bare function calls at module level — runs on import[/dim]")
    console.print()

    cap = 10
    s, icon = SEVERITY_STYLE[sev]
    if ctx.verbose:
        for se in items[:cap]:
            rel = se.filepath.relative_to(ctx.project_root)
            loc = f"{rel}:{se.lineno}"
            console.print(f"{I2}[{s}]{icon}[/{s}] [bold]{loc:<30s}[/bold] [yellow]{se.call_text}[/yellow]")
        if len(items) > cap:
            console.print(f"{I2}[dim]... and {len(items) - cap} more[/dim]")
    else:
        by_file: dict[str, list[str]] = defaultdict(list)
        for se in items:
            rel = str(se.filepath.relative_to(ctx.project_root))
            by_file[rel].append(se.call_text)
        file_items = list(by_file.items())
        for filepath, calls in file_items[:cap]:
            call_summary = ", ".join(calls[:3])
            extra = f" +{len(calls) - 3} more" if len(calls) > 3 else ""
            console.print(
                f"{I2}[{s}]{icon}[/{s}] [bold]{filepath:<30s}[/bold] [yellow]{call_summary}{extra}[/yellow]"
            )
        if len(file_items) > cap:
            console.print(f"{I2}[dim]... and {len(file_items) - cap} more files[/dim]")


def json_view(items: list[SideEffect], ctx: AnalysisContext) -> list[dict]:
    return [
        {
            "file": str(se.filepath.relative_to(ctx.project_root)),
            "lineno": se.lineno,
            "call_text": se.call_text,
        }
        for se in items
    ]


views("side_effects", rich=rich_view, json=json_view, findings=to_findings)


# ── Helpers ──────────────────────────────────────────────────────────


def _get_call_name(node: ast.Call) -> str | None:
    return _get_dotted_name(node.func)


def _get_dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _get_dotted_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None
