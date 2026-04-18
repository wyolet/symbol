"""Entry point detection — find where a project starts executing."""

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ca.symbol.shared.context import AnalysisContext
from ca.symbol.shared.registry import register, views

I1, I2 = "  ", "    "


@dataclass
class EntryPoint:
    filepath: Path
    lineno: int
    description: str
    in_main_guard: bool


@register(
    name="entrypoints",
    description="files with if __name__ == '__main__' guards",
    kind="file",
    contributes_to_report=False,
    priority=30,
)
def detect(
    ctx: AnalysisContext,
    filepath: Path,
    tree: ast.Module | None,
) -> list[EntryPoint]:
    if tree is None:
        return []

    results: list[EntryPoint] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If) and _is_main_guard(node):
            calls = _find_calls_in_block(node)
            if calls:
                for call_desc, lineno in calls:
                    results.append(EntryPoint(
                        filepath=filepath, lineno=lineno,
                        description=call_desc, in_main_guard=True,
                    ))
            else:
                results.append(EntryPoint(
                    filepath=filepath, lineno=node.lineno,
                    description="if __name__ == '__main__'", in_main_guard=True,
                ))
    return results


# ── Views ────────────────────────────────────────────────────────────


def rich_view(items: list[EntryPoint], ctx: AnalysisContext, console: Console) -> None:
    console.print()
    console.print(Text(f"{I1}\U0001f680 ENTRY POINTS ({len(items)})", style="bold blue"))
    console.print()

    if not items:
        console.print(f"{I2}[dim](none found)[/dim]")
        return

    if ctx.verbose:
        for ep in items:
            rel = ep.filepath.relative_to(ctx.project_root)
            loc = f"{rel}:{ep.lineno}"
            guard = " [dim]\u2190 if __name__[/dim]" if ep.in_main_guard else ""
            console.print(f"{I2}[green]\u2713[/green] [bold]{loc:<30s}[/bold] {ep.description}{guard}")
    else:
        by_file: dict[Path, list[EntryPoint]] = defaultdict(list)
        for ep in items:
            by_file[ep.filepath].append(ep)
        for filepath, eps in by_file.items():
            rel = filepath.relative_to(ctx.project_root)
            calls = ", ".join(ep.description for ep in eps)
            console.print(f"{I2}[green]\u2713[/green] [bold]{str(rel):<30s}[/bold] {calls}")


def json_view(items: list[EntryPoint], ctx: AnalysisContext) -> list[dict]:
    return [
        {
            "file": str(ep.filepath.relative_to(ctx.project_root)),
            "lineno": ep.lineno,
            "description": ep.description,
        }
        for ep in items
    ]


views("entrypoints", rich=rich_view, json=json_view)


# ── AST helpers ──────────────────────────────────────────────────────


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    if len(test.comparators) != 1:
        return False
    left = test.left
    right = test.comparators[0]
    return (_is_name(left, "__name__") and _is_str(right, "__main__")) or (
        _is_str(left, "__main__") and _is_name(right, "__name__")
    )


def _is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_str(node: ast.expr, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _find_calls_in_block(node: ast.AST) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            call_name = _get_call_name(child)
            if call_name:
                calls.append((f"{call_name}()", child.lineno))
    return calls


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
