"""Code structure analysis — functions, classes, type hints from AST."""

import ast
from dataclasses import dataclass
from pathlib import Path

from ca_tools.shared.ast_cache import ASTCache


@dataclass
class CodeStructure:
    """Aggregate code structure metrics across a project."""

    functions: int = 0  # top-level functions (not methods)
    methods: int = 0  # functions inside classes
    classes: int = 0
    total_callable_lines: int = 0  # sum of function/method body lines (for avg)
    typed_functions: int = 0  # functions/methods with return annotation
    typed_args: int = 0  # args with type annotation (excluding self/cls)
    total_args: int = 0  # total args (excluding self/cls)
    typed_attrs: int = 0  # class attributes with type annotation
    total_attrs: int = 0  # total class attributes
    typed_vars: int = 0  # standalone variables with type annotation
    total_vars: int = 0  # total standalone variables (module-level + function-level)

    @property
    def total_callables(self) -> int:
        return self.functions + self.methods

    @property
    def avg_function_lines(self) -> int:
        return self.total_callable_lines // self.total_callables if self.total_callables else 0

    @property
    def type_coverage_pct(self) -> float:
        """Percentage of functions/methods that have a return type annotation."""
        if not self.total_callables:
            return 0.0
        return self.typed_functions / self.total_callables * 100

    @property
    def arg_coverage_pct(self) -> float:
        """Percentage of args (excluding self/cls) that have type annotations."""
        if not self.total_args:
            return 0.0
        return self.typed_args / self.total_args * 100

    @property
    def attr_coverage_pct(self) -> float:
        """Percentage of class attributes that have type annotations."""
        if not self.total_attrs:
            return 0.0
        return self.typed_attrs / self.total_attrs * 100


def detect_code_structure(
    project_root: Path,
    cache: ASTCache,
) -> CodeStructure:
    """Analyze code structure from already-parsed ASTs."""
    result = CodeStructure()

    for py_file in cache.files:
        tree = cache.get_ast(py_file)
        if tree is None:
            continue
        _analyze_module(tree, result)

    return result


def _analyze_module(tree: ast.Module, result: CodeStructure) -> None:
    """Walk a module's top-level nodes to collect structure metrics."""
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
            # Module-level typed variable: BASE_URL: str = "..."
            result.total_vars += 1
            result.typed_vars += 1
        elif isinstance(node, ast.Assign):
            # Module-level untyped variable: BASE_URL = "..."
            result.total_vars += 1


def _count_function(node: ast.FunctionDef | ast.AsyncFunctionDef, result: CodeStructure, is_method: bool) -> None:
    """Count a single function/method and its type annotations."""
    if is_method:
        result.methods += 1
    else:
        result.functions += 1

    # Function body size (end_lineno - lineno + 1, if available)
    if node.end_lineno and node.lineno:
        result.total_callable_lines += node.end_lineno - node.lineno + 1

    # Return type annotation
    if node.returns:
        result.typed_functions += 1

    # Arg annotations (skip self/cls for methods)
    args = node.args
    all_args = args.args + args.posonlyargs + args.kwonlyargs
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)

    for i, arg in enumerate(all_args):
        # Skip self/cls (first arg of methods)
        if is_method and i == 0 and arg.arg in ("self", "cls"):
            continue
        result.total_args += 1
        if arg.annotation:
            result.typed_args += 1
