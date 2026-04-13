"""Swallowed exception detector — find except blocks that silently ignore errors."""

import ast
from dataclasses import dataclass
from pathlib import Path

from ca_tools.shared.ast_cache import ASTCache


@dataclass
class SwallowedException:
    filepath: Path
    line: int
    exception_type: str  # "Exception", "bare except", specific type
    context: str  # enclosing function/class name


def detect_swallowed(
    project_root: Path,
    cache: ASTCache,
) -> list[SwallowedException]:
    """Find except blocks that swallow errors with pass, ..., or just a comment."""
    results: list[SwallowedException] = []

    for py_file in cache.files:
        tree = cache.get_ast(py_file)
        if tree is None:
            continue
        _walk_for_swallowed(tree, py_file, results)

    results.sort(key=lambda s: (str(s.filepath), s.line))
    return results


def _walk_for_swallowed(tree: ast.Module, filepath: Path, results: list[SwallowedException]) -> None:
    """Walk AST looking for try/except with empty handlers."""
    _walk_body(tree.body, filepath, results, context="<module>")


def _walk_body(body: list[ast.stmt], filepath: Path, results: list[SwallowedException], context: str) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_body(node.body, filepath, results, context=node.name)
        elif isinstance(node, ast.ClassDef):
            _walk_body(node.body, filepath, results, context=node.name)
        elif isinstance(node, ast.Try):
            for handler in node.handlers:
                if _is_swallowed(handler):
                    exc_type = _handler_type(handler)
                    results.append(SwallowedException(
                        filepath=filepath,
                        line=handler.lineno,
                        exception_type=exc_type,
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
    """Check if an except handler swallows the exception.

    Swallowed = body is just `pass`, `...`, or a bare string (comment-like docstring).
    """
    body = handler.body
    if len(body) == 0:
        return True
    if len(body) == 1:
        stmt = body[0]
        # pass
        if isinstance(stmt, ast.Pass):
            return True
        # ... (Ellipsis)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
            return True
        # bare string (docstring used as comment)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            return True
    return False


def _handler_type(handler: ast.ExceptHandler) -> str:
    """Extract the exception type name from a handler."""
    if handler.type is None:
        return "bare except"
    if isinstance(handler.type, ast.Name):
        return handler.type.id
    if isinstance(handler.type, ast.Attribute):
        # e.g. os.error
        parts = []
        node = handler.type
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))
    if isinstance(handler.type, ast.Tuple):
        # except (TypeError, ValueError):
        names = []
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
        return ", ".join(names) if names else "multiple"
    return "unknown"
