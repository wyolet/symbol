"""Side effect detection — find bare function calls at module level."""

import ast
from dataclasses import dataclass
from pathlib import Path

from ca_tools.shared.ast_cache import ASTCache
from ca_tools.shared.files import collect_py_files
from ca_tools.shared.spec import SideEffectSpec, Spec


@dataclass
class SideEffect:
    filepath: Path
    lineno: int
    call_text: str


def detect_sideeffects(
    project_root: Path,
    spec: Spec,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache: ASTCache | None = None,
) -> list[SideEffect]:
    results: list[SideEffect] = []

    if cache:
        files = cache.files
    else:
        files = collect_py_files(project_root, include, exclude)

    for py_file in files:
        if cache:
            tree = cache.get_ast(py_file)
        else:
            try:
                source = py_file.read_text()
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError, OSError):
                tree = None

        if tree is None:
            continue
        results.extend(_find_sideeffects_in_file(tree, py_file, spec.side_effects))

    return results


def _find_sideeffects_in_file(
    tree: ast.Module,
    filepath: Path,
    se: SideEffectSpec,
) -> list[SideEffect]:
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
        if leaf_name in se.safe_calls:
            continue

        if leaf_name.startswith("_") or leaf_name[0].isupper():
            if call_name not in se.known_effects:
                continue

        results.append(SideEffect(filepath=filepath, lineno=node.lineno, call_text=f"{call_name}()"))

    return results


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
