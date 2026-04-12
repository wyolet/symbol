"""Entry point detection — find where a project starts executing."""

import ast
from dataclasses import dataclass
from pathlib import Path

from ca_tools.shared.files import collect_py_files


@dataclass
class EntryPoint:
    filepath: Path
    lineno: int
    description: str
    in_main_guard: bool


def detect_entrypoints(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[EntryPoint]:
    """Find all entry points in a Python project."""
    results: list[EntryPoint] = []

    for py_file in collect_py_files(project_root, include, exclude):
        try:
            source = py_file.read_text()
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        results.extend(_find_entrypoints_in_file(tree, py_file))

    return results


def _find_entrypoints_in_file(tree: ast.Module, filepath: Path) -> list[EntryPoint]:
    results: list[EntryPoint] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If) and _is_main_guard(node):
            calls = _find_calls_in_block(node)
            if calls:
                for call_desc, lineno in calls:
                    results.append(
                        EntryPoint(filepath=filepath, lineno=lineno, description=call_desc, in_main_guard=True)
                    )
            else:
                results.append(
                    EntryPoint(
                        filepath=filepath,
                        lineno=node.lineno,
                        description="if __name__ == '__main__'",
                        in_main_guard=True,
                    )
                )

    return results


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
