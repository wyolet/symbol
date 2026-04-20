"""Code structure analysis — functions, classes, type hints from AST."""

import ast
from dataclasses import dataclass

from rich.console import Console
from rich.text import Text

from wyolet.symbol.shared.context import AnalysisContext
from wyolet.symbol.shared.registry import register, views

I1, I2 = "  ", "    "


@dataclass
class CodeStructure:
    """Aggregate code structure metrics across a project."""

    functions: int = 0
    methods: int = 0
    classes: int = 0
    total_callable_lines: int = 0
    typed_functions: int = 0
    typed_args: int = 0
    total_args: int = 0
    typed_attrs: int = 0
    total_attrs: int = 0
    typed_vars: int = 0
    total_vars: int = 0

    @property
    def total_callables(self) -> int:
        return self.functions + self.methods

    @property
    def avg_function_lines(self) -> int:
        return self.total_callable_lines // self.total_callables if self.total_callables else 0

    @property
    def type_coverage_pct(self) -> float:
        if not self.total_callables:
            return 0.0
        return self.typed_functions / self.total_callables * 100

    @property
    def arg_coverage_pct(self) -> float:
        if not self.total_args:
            return 0.0
        return self.typed_args / self.total_args * 100

    @property
    def attr_coverage_pct(self) -> float:
        if not self.total_attrs:
            return 0.0
        return self.typed_attrs / self.total_attrs * 100


@register(
    name="code_structure",
    description="code structure metrics — functions, classes, type coverage",
    kind="project",
    contributes_to_report=False,
    priority=20,
)
def detect(ctx: AnalysisContext) -> list[CodeStructure]:
    """Returns a single-element list with aggregate CodeStructure."""
    result = CodeStructure()

    for py_file in ctx.cache.files:
        tree = ctx.cache.get_ast(py_file)
        if tree is None:
            continue
        _analyze_module(tree, result)

    return [result]


# ── Views ────────────────────────────────────────────────────────────


def _pct_style(pct: float) -> str:
    if pct == 0:
        return "red"
    if pct < 50:
        return "yellow"
    if pct < 80:
        return "cyan"
    return "green"


def rich_view(items: list[CodeStructure], ctx: AnalysisContext, console: Console) -> None:
    if not items:
        return
    cs = items[0]

    # Type coverage (shown separately from shape — shape is in cli.py with LOC)
    if cs.total_callables == 0:
        return

    total_typed = cs.typed_functions + cs.typed_args + cs.typed_attrs
    total_checkable = cs.total_callables + cs.total_args + cs.total_attrs
    overall_pct = (total_typed / total_checkable * 100) if total_checkable else 0
    overall_style = _pct_style(overall_pct)

    console.print()
    console.print(Text(f"{I1}\U0001f3f7\ufe0f  TYPE COVERAGE", style="bold"))
    console.print()

    parts = [f"[{overall_style} bold]{overall_pct:.0f}%[/{overall_style} bold] [dim]overall[/dim]"]
    parts.append(f"[{_pct_style(cs.type_coverage_pct)}]{cs.type_coverage_pct:.0f}%[/{_pct_style(cs.type_coverage_pct)}] [dim]functions[/dim]")
    parts.append(f"[{_pct_style(cs.arg_coverage_pct)}]{cs.arg_coverage_pct:.0f}%[/{_pct_style(cs.arg_coverage_pct)}] [dim]args[/dim]")
    if cs.total_attrs > 0:
        parts.append(f"[{_pct_style(cs.attr_coverage_pct)}]{cs.attr_coverage_pct:.0f}%[/{_pct_style(cs.attr_coverage_pct)}] [dim]attrs[/dim]")
    console.print(f"{I2}{'  '.join(parts)}")

    if cs.total_vars > 0:
        console.print(f"{I2}[dim]{cs.typed_vars}/{cs.total_vars} variables typed (not in overall — inferred by type checkers)[/dim]")


def json_view(items: list[CodeStructure], ctx: AnalysisContext) -> dict:
    if not items:
        return {}
    cs = items[0]
    return {
        "functions": cs.functions,
        "methods": cs.methods,
        "classes": cs.classes,
        "avg_function_lines": cs.avg_function_lines,
        "type_coverage_pct": round(cs.type_coverage_pct, 1),
        "typed_functions": cs.typed_functions,
        "total_callables": cs.total_callables,
        "arg_coverage_pct": round(cs.arg_coverage_pct, 1),
        "typed_args": cs.typed_args,
        "total_args": cs.total_args,
        "attr_coverage_pct": round(cs.attr_coverage_pct, 1),
        "typed_attrs": cs.typed_attrs,
        "total_attrs": cs.total_attrs,
    }


views("code_structure", rich=rich_view, json=json_view)


# ── AST analysis ─────────────────────────────────────────────────────


def _analyze_module(tree: ast.Module, result: CodeStructure) -> None:
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _count_function(node, result, is_method=False)
        elif isinstance(node, ast.ClassDef):
            result.classes += 1
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _count_function(child, result, is_method=True)
                elif isinstance(child, ast.AnnAssign):
                    result.total_attrs += 1
                    result.typed_attrs += 1
                elif isinstance(child, ast.Assign):
                    result.total_attrs += 1
        elif isinstance(node, ast.AnnAssign):
            result.total_vars += 1
            result.typed_vars += 1
        elif isinstance(node, ast.Assign):
            result.total_vars += 1


def _count_function(node: ast.FunctionDef | ast.AsyncFunctionDef, result: CodeStructure, is_method: bool) -> None:
    if is_method:
        result.methods += 1
    else:
        result.functions += 1

    if node.end_lineno and node.lineno:
        result.total_callable_lines += node.end_lineno - node.lineno + 1

    if node.returns:
        result.typed_functions += 1

    args = node.args
    all_args = args.args + args.posonlyargs + args.kwonlyargs
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)

    for i, arg in enumerate(all_args):
        if is_method and i == 0 and arg.arg in ("self", "cls"):
            continue
        result.total_args += 1
        if arg.annotation:
            result.typed_args += 1
