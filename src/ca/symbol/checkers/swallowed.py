"""Swallowed exception checker — find except blocks that silently ignore errors."""

import ast
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ca.symbol.shared.context import AnalysisContext
from ca.symbol.shared.registry import register, views
from ca.symbol.shared.findings import Finding, Severity

I1, I2 = "  ", "    "


@dataclass
class SwallowedException:
    filepath: Path
    line: int
    exception_type: str
    context: str


@register(
    name="swallowed",
    description="except blocks that silently ignore errors",
    kind="file",
    default_severity=Severity.ERROR,
    priority=70,
)
def detect(
    ctx: AnalysisContext,
    filepath: Path,
    tree: ast.Module | None,
) -> list[SwallowedException]:
    if tree is None:
        return []
    results: list[SwallowedException] = []
    _walk_body(tree.body, filepath, results, context="<module>")
    return results


# ── Views ────────────────────────────────────────────────────────────


def to_findings(items: list[SwallowedException], ctx: AnalysisContext) -> list[Finding]:
    return [
        Finding(
            section="swallowed",
            message=f"except {sw.exception_type}: pass",
            severity=Severity.ERROR,
            location=f"{sw.filepath.relative_to(ctx.project_root)}:{sw.line}",
        )
        for sw in items
    ]


def rich_view(items: list[SwallowedException], ctx: AnalysisContext, console: Console) -> None:
    if not items:
        return
    console.print()
    console.print(Text(f"{I1}\U0001f635 SWALLOWED EXCEPTIONS ({len(items)})", style="bold red"))
    console.print(f"{I2}[dim]except blocks that silently ignore errors — bugs hide here[/dim]")
    console.print()
    cap = 10
    for sw in items[:cap]:
        rel = sw.filepath.relative_to(ctx.project_root)
        loc = f"{rel}:{sw.line}"
        console.print(
            f"{I2}[red]\u2717[/red] [bold]{loc:<40s}[/bold] "
            f"[red]{sw.exception_type}[/red] [dim]in {sw.context}[/dim]"
        )
    if len(items) > cap:
        console.print(f"{I2}[dim]... and {len(items) - cap} more[/dim]")


def json_view(items: list[SwallowedException], ctx: AnalysisContext) -> list[dict]:
    return [
        {
            "file": str(sw.filepath.relative_to(ctx.project_root)),
            "line": sw.line,
            "exception_type": sw.exception_type,
            "context": sw.context,
        }
        for sw in items
    ]


views("swallowed", rich=rich_view, json=json_view, findings=to_findings)


# ── AST walking ──────────────────────────────────────────────────────


def _walk_body(
    body: list[ast.stmt],
    filepath: Path,
    results: list[SwallowedException],
    context: str,
) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_body(node.body, filepath, results, context=node.name)
        elif isinstance(node, ast.ClassDef):
            _walk_body(node.body, filepath, results, context=node.name)
        elif isinstance(node, ast.Try):
            for handler in node.handlers:
                if _is_swallowed(handler):
                    results.append(SwallowedException(
                        filepath=filepath,
                        line=handler.lineno,
                        exception_type=_handler_type(handler),
                        context=context,
                    ))
            _walk_body(node.body, filepath, results, context=context)
            _walk_body(node.orelse, filepath, results, context=context)
            _walk_body(node.finalbody, filepath, results, context=context)
        elif isinstance(node, ast.If):
            _walk_body(node.body, filepath, results, context=context)
            _walk_body(node.orelse, filepath, results, context=context)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            _walk_body(node.body, filepath, results, context=context)
            _walk_body(node.orelse, filepath, results, context=context)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            _walk_body(node.body, filepath, results, context=context)


def _is_swallowed(handler: ast.ExceptHandler) -> bool:
    body = handler.body
    if len(body) == 0:
        return True
    if len(body) == 1:
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            return True
    return False


def _handler_type(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare except"
    if isinstance(handler.type, ast.Name):
        return handler.type.id
    if isinstance(handler.type, ast.Attribute):
        parts = []
        node = handler.type
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))
    if isinstance(handler.type, ast.Tuple):
        names = []
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
        return ", ".join(names) if names else "multiple"
    return "unknown"
