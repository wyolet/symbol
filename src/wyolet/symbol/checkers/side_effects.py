"""Side effect detection — find bare function calls at module level."""

import ast
import fnmatch
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from wyolet.symbol.shared.context import AnalysisContext
from wyolet.symbol.shared.registry import register, views
from wyolet.symbol.shared.findings import SEVERITY_STYLE, Finding, Severity

I1, I2 = "  ", "    "

_STYLE_MAP = {
    Severity.SKIP: "dim",
    Severity.DEBUG: "dim",
    Severity.INFO: "bold blue",
    Severity.WARNING: "bold yellow",
    Severity.ERROR: "bold red",
    Severity.CRITICAL: "bold red",
}
_ICON_MAP = {
    Severity.SKIP: "○",
    Severity.DEBUG: "\u00b7",
    Severity.INFO: "\u2139\ufe0f ",
    Severity.WARNING: "\u26a0\ufe0f ",
    Severity.ERROR: "\U0001f534",
    Severity.CRITICAL: "\U0001f6a8",
}


@dataclass
class SideEffect:
    filepath: Path
    lineno: int
    call_text: str
    severity: Severity = Severity.WARNING


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
    known_effects = ctx.resolved.error_calls
    ignore_patterns = ctx.resolved.ignore_patterns.get("side_effects", [])
    file_roles = ctx.resolved.side_effect_patterns
    package_roles = ctx.resolved.side_effect_package_roles

    # Severity for this file based on its role
    file_sev = file_roles.get(filepath.name, Severity.WARNING)

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

        # Resolve severity: package role wins over file role
        sev = _resolve_severity(call_name, file_sev, package_roles)

        results.append(SideEffect(filepath=filepath, lineno=node.lineno, call_text=call_text, severity=sev))

    return results


def _resolve_severity(
    call_name: str,
    file_sev: Severity,
    package_roles: dict[str, Severity],
) -> Severity:
    """Package role wins over file role. Highest severity among matched prefixes wins."""
    pkg_sev = None
    for prefix, sev in package_roles.items():
        if call_name == prefix or call_name.startswith(prefix + "."):
            if pkg_sev is None or sev > pkg_sev:
                pkg_sev = sev
    return pkg_sev if pkg_sev is not None else file_sev


# ── Views ────────────────────────────────────────────────────────────


def to_findings(items: list[SideEffect], ctx: AnalysisContext) -> list[Finding]:
    # Only contribute findings at WARNING and above to the report
    return [
        Finding(
            section="side_effects",
            message=se.call_text,
            severity=se.severity,
            location=f"{se.filepath.relative_to(ctx.project_root)}:{se.lineno}",
        )
        for se in items
        if se.severity >= Severity.WARNING
    ]


def rich_view(items: list[SideEffect], ctx: AnalysisContext, console: Console) -> None:
    # Group by severity, hide DEBUG unless verbose
    visible = [se for se in items if ctx.verbose or se.severity >= Severity.WARNING]

    console.print()
    if not visible:
        console.print(Text(f"{I1}\u2705 SIDE EFFECTS (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No module-level side effects detected[/green]")
        return

    # Header severity = worst among visible items
    worst = max(se.severity for se in visible)
    console.print(Text(f"{I1}{_ICON_MAP[worst]} SIDE EFFECTS ({len(visible)})", style=_STYLE_MAP[worst]))
    console.print(f"{I2}[dim]Bare function calls at module level — runs on import[/dim]")
    console.print()

    cap = 10
    if ctx.verbose:
        shown = visible[:cap]
        for se in shown:
            s, icon = SEVERITY_STYLE[se.severity]
            rel = se.filepath.relative_to(ctx.project_root)
            console.print(
                f"{I2}[{s}]{icon}[/{s}] [bold]{str(rel) + ':' + str(se.lineno):<40s}[/bold] "
                f"[yellow]{se.call_text}[/yellow]"
            )
        if len(visible) > cap:
            console.print(f"{I2}[dim]... and {len(visible) - cap} more[/dim]")
    else:
        # Group by file, show worst severity per file
        by_file: dict[str, list[SideEffect]] = defaultdict(list)
        for se in visible:
            by_file[str(se.filepath.relative_to(ctx.project_root))].append(se)

        file_items = list(by_file.items())
        for rel_path, ses in file_items[:cap]:
            file_worst = max(se.severity for se in ses)
            s, icon = SEVERITY_STYLE[file_worst]
            call_summary = ", ".join(se.call_text for se in ses[:3])
            extra = f" +{len(ses) - 3} more" if len(ses) > 3 else ""
            console.print(
                f"{I2}[{s}]{icon}[/{s}] [bold]{rel_path:<40s}[/bold] [yellow]{call_summary}{extra}[/yellow]"
            )
        if len(file_items) > cap:
            console.print(f"{I2}[dim]... and {len(file_items) - cap} more files[/dim]")


def json_view(items: list[SideEffect], ctx: AnalysisContext) -> list[dict]:
    return [
        {
            "file": str(se.filepath.relative_to(ctx.project_root)),
            "lineno": se.lineno,
            "call_text": se.call_text,
            "severity": se.severity.value,
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
