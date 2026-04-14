"""Import graph construction and orphan file detection."""

import ast
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ca_tools.shared.ast_cache import ASTCache
from ca_tools.shared.files import collect_py_files


class ImportScope(Enum):
    """Where in the file an import lives — determines how we treat it."""

    TOP = "top"  # Module-level: real runtime edge
    TYPE_CHECKING = "type_checking"  # if TYPE_CHECKING: — not runtime, skip
    DEFERRED = "deferred"  # Inside function/method body — lazy import, code smell


@dataclass
class ImportEdge:
    """A single import statement, parsed from AST."""

    module: str  # The resolved module dotted path (what Python loads)
    names: list[str]  # Imported names (potential submodules or classes)
    scope: ImportScope  # Where it lives in the file
    line: int  # Source line number


@dataclass
class ImportGraph:
    files: list[Path] = field(default_factory=list)
    edges: dict[Path, list[ImportEdge]] = field(default_factory=dict)
    resolved_edges: dict[Path, set[Path]] = field(default_factory=dict)
    module_to_file: dict[str, Path] = field(default_factory=dict)


@dataclass
class OrphanFile:
    filepath: Path
    reason: str


def build_import_graph(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache: ASTCache | None = None,
    propagate_init: bool = True,
) -> ImportGraph:
    graph = ImportGraph()

    if cache:
        py_files = cache.files
    else:
        py_files = collect_py_files(project_root, include, exclude)
    graph.files = py_files

    module_to_file = _build_module_map(project_root, py_files)
    graph.module_to_file = module_to_file

    for py_file in py_files:
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

        import_edges = _extract_imports(tree, py_file, project_root)
        graph.edges[py_file] = import_edges

        # Resolve following Python's import rules:
        # 1. For each imported name, check if it resolves as a submodule file
        # 2. Only add the module (package) edge if some names are NOT submodules
        #    (meaning they must come from __init__.py or the module itself)
        #
        # This matches Python's behavior: `from pkg import sub` where sub is a
        # submodule loads sub.py, not __init__.py. But `from pkg import MyClass`
        # loads __init__.py because MyClass is defined there.
        resolved: set[Path] = set()
        for edge in import_edges:
            if edge.scope == ImportScope.TYPE_CHECKING:
                continue

            if not edge.names:
                # `import foo.bar` — no names, just the module
                if edge.module and edge.module in module_to_file:
                    resolved.add(module_to_file[edge.module])
                continue

            # `from foo import bar, baz` — check each name
            has_non_submodule = False
            for name in edge.names:
                submodule = f"{edge.module}.{name}" if edge.module else name
                if submodule in module_to_file:
                    # name is a submodule file — edge goes to that file
                    resolved.add(module_to_file[submodule])
                else:
                    # name is a class/function — must come from the module itself
                    has_non_submodule = True

            # Only add edge to the module if it has non-submodule names
            # (those names must be defined in the module's own code)
            if has_non_submodule and edge.module and edge.module in module_to_file:
                resolved.add(module_to_file[edge.module])

        resolved.discard(py_file)
        graph.resolved_edges[py_file] = resolved

    # Propagate __init__.py re-exports for orphan reachability.
    # Not needed for cycle/map analysis — those use direct edges only.
    if propagate_init:
        _resolve_submodule_imports(graph)

    return graph


def _build_module_map(project_root: Path, py_files: list[Path]) -> dict[str, Path]:
    module_to_file: dict[str, Path] = {}

    for py_file in py_files:
        rel = py_file.relative_to(project_root)
        parts = list(rel.parts)

        if parts[-1] == "__init__.py":
            module_parts = parts[:-1]
        else:
            module_parts = parts[:-1] + [parts[-1].removesuffix(".py")]

        if module_parts:
            module_name = ".".join(module_parts)
            module_to_file[module_name] = py_file

            # Handle src/ layout at any depth: strip first "src" segment
            # Works for both root src/ and nested like api/src/
            for i, part in enumerate(module_parts):
                if part == "src" and i < len(module_parts) - 1:
                    alt_name = ".".join(module_parts[i + 1 :])
                    module_to_file[alt_name] = py_file
                    # Also register with prefix before src/
                    if i > 0:
                        prefix = ".".join(module_parts[:i])
                        module_to_file[f"{prefix}.{alt_name}"] = py_file
                    break

    return module_to_file


def _is_type_checking_block(node: ast.If) -> bool:
    """Check if an `if` node is `if TYPE_CHECKING:` guard."""
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False



def _extract_imports(tree: ast.Module, filepath: Path, project_root: Path) -> list[ImportEdge]:
    """Extract structured import edges from an AST.

    Follows Python's import rules:
    - `import foo.bar` → module="foo.bar", names=[]
    - `from foo.bar import Baz` → module="foo.bar", names=["Baz"]
    - `from . import bar` → module=<current_package>, names=["bar"]
    - `from .bar import baz` → module=<current_package>.bar, names=["baz"]

    Tracks scope: top-level, TYPE_CHECKING, or deferred (function body).
    """
    edges: list[ImportEdge] = []

    # Compute this file's package for relative import resolution
    rel = filepath.relative_to(project_root)
    rel_parts = list(rel.parts)
    if rel_parts[-1] == "__init__.py":
        file_package_parts = rel_parts[:-1]
    else:
        file_package_parts = rel_parts[:-1]

    def _resolve_relative(level: int, module: str | None) -> str:
        """Resolve a relative import to an absolute dotted path."""
        base_parts = file_package_parts[: len(file_package_parts) - (level - 1)]
        if module:
            return ".".join(base_parts + module.split("."))
        return ".".join(base_parts)

    def _walk_body(body: list[ast.stmt], scope: ImportScope) -> None:
        """Walk a list of statements, tracking scope transitions."""
        for node in body:
            # TYPE_CHECKING block
            if isinstance(node, ast.If) and _is_type_checking_block(node):
                _walk_body(node.body, ImportScope.TYPE_CHECKING)
                if node.orelse:
                    _walk_body(node.orelse, scope)
                continue

            # Function/method body — deferred imports
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _walk_body(node.body, ImportScope.DEFERRED)
                continue

            # Class body — imports here are still top-level (class init time)
            if isinstance(node, ast.ClassDef):
                _walk_body(node.body, scope)
                continue

            # Regular if/else, try/except, with — inherit scope
            if isinstance(node, ast.If):
                _walk_body(node.body, scope)
                if node.orelse:
                    _walk_body(node.orelse, scope)
                continue
            if isinstance(node, ast.Try):
                _walk_body(node.body, scope)
                for handler in node.handlers:
                    _walk_body(handler.body, scope)
                _walk_body(node.orelse, scope)
                _walk_body(node.finalbody, scope)
                continue
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                _walk_body(node.body, scope)
                if node.orelse:
                    _walk_body(node.orelse, scope)
                continue
            if isinstance(node, (ast.With, ast.AsyncWith)):
                _walk_body(node.body, scope)
                continue

            # Import statements
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # import foo.bar → module is the full dotted path
                    edges.append(ImportEdge(
                        module=alias.name,
                        names=[],
                        scope=scope,
                        line=node.lineno,
                    ))

            elif isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
                if node.level == 0:
                    # Absolute: from foo.bar import Baz
                    edges.append(ImportEdge(
                        module=node.module or "",
                        names=names,
                        scope=scope,
                        line=node.lineno,
                    ))
                else:
                    # Relative: from .bar import baz
                    resolved = _resolve_relative(node.level, node.module)
                    edges.append(ImportEdge(
                        module=resolved,
                        names=names,
                        scope=scope,
                        line=node.lineno,
                    ))

    _walk_body(tree.body, ImportScope.TOP)
    return edges


def _resolve_submodule_imports(graph: ImportGraph) -> None:
    """Second pass: when __init__.py is reached, also mark its re-exported submodules as reached.

    If file A imports package P (resolved to P/__init__.py), and P/__init__.py
    imports from .foo, then P/foo.py is transitively connected through A → P → foo.
    """
    # Build set of all __init__.py files
    init_files = {f for f in graph.files if f.name == "__init__.py"}

    # For each __init__.py that is imported by something, propagate to its submodules
    changed = True
    while changed:
        changed = False
        imported_files: set[Path] = set()
        for targets in graph.resolved_edges.values():
            imported_files.update(targets)

        for init_file in init_files:
            if init_file not in imported_files:
                continue
            # Get what __init__.py imports
            init_targets = graph.resolved_edges.get(init_file, set())
            # For every file that imports this __init__.py, add __init__'s targets
            for _src, targets in graph.resolved_edges.items():
                if init_file in targets:
                    new_targets = init_targets - targets
                    if new_targets:
                        targets.update(new_targets)
                        changed = True


def graph_summary(graph: ImportGraph, orphan_count: int) -> dict[str, int]:
    total = len(graph.files)
    connected: set[Path] = set()
    for src, targets in graph.resolved_edges.items():
        if targets:
            connected.add(src)
            connected.update(targets)

    return {
        "total": total,
        "connected": len(connected),
        "orphans": orphan_count,
        "longest_chain": _longest_chain(graph),
    }


def _longest_chain(graph: ImportGraph) -> int:
    memo: dict[Path, int] = {}
    visiting: set[Path] = set()

    def dfs(node: Path) -> int:
        if node in memo:
            return memo[node]
        if node in visiting:
            return 0
        visiting.add(node)
        max_depth = 0
        for target in graph.resolved_edges.get(node, set()):
            max_depth = max(max_depth, dfs(target))
        visiting.discard(node)
        memo[node] = max_depth + 1
        return memo[node]

    return max((dfs(f) for f in graph.files), default=0)
