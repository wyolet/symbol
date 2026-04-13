"""Per-file analysis — exports, imports, per-name blast radius."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

from ca_tools.audit.orphans import ImportEdge, ImportGraph, ImportScope, build_import_graph


@dataclass
class MethodInfo:
    """A method inside a class."""

    name: str
    line: int
    lines: int = 0
    complexity: int = 0
    max_depth: int = 0
    is_async: bool = False


@dataclass
class ExportedName:
    """A name defined at module level — function, class, or variable."""

    name: str
    kind: str  # "function", "async function", "class", "variable"
    line: int
    lines: int = 0  # body length
    complexity: int = 0  # cyclomatic complexity
    max_depth: int = 0  # max nesting depth
    used_by: list[str] = field(default_factory=list)  # relative paths of importers
    internal_refs: int = 0  # how many times referenced within the same file (excluding definition)
    methods: list[MethodInfo] = field(default_factory=list)  # for classes only


@dataclass
class ImportedName:
    """A name this file imports from another module."""

    name: str
    source_module: str  # dotted module path
    source_file: str  # resolved relative file path, or "" if external
    scope: str  # "top", "type_checking", "deferred"


@dataclass
class FileAnalysis:
    """Complete analysis of a single file."""

    path: str  # relative path
    lines: int
    sloc: int
    classes: int
    functions: int
    typed_pct: float  # return type coverage
    total_complexity: int = 0  # sum of all function CCs
    max_depth: int = 0  # deepest nesting in the file

    exports: list[ExportedName] = field(default_factory=list)
    imports: list[ImportedName] = field(default_factory=list)
    direct_importers: int = 0  # files that import this file
    transitive_importers: int = 0  # files that transitively depend on this file


def analyze_file(
    project_root: Path,
    target: str,
    graph: ImportGraph | None = None,
    cache: "ASTCache | None" = None,
) -> FileAnalysis | None:
    """Analyze a single file within a project.

    If graph is provided, uses it directly (avoids rebuilding for batch analysis).
    If not, builds the import graph from scratch.
    If cache is provided, uses it for AST parsing (avoids re-parsing).
    """
    if graph is None:
        graph = build_import_graph(project_root, cache=cache, skip_defaults=False, propagate_init=False)

    # Resolve target to a file
    target_path = _resolve_target(project_root, target, graph.files)
    if not target_path:
        return None

    rel = str(target_path.relative_to(project_root))

    # Read and parse the target file (use cache if available)
    if cache:
        tree = cache.get_ast(target_path)
        if tree is None:
            return None
        try:
            source = target_path.read_text()
        except OSError:
            return None
    else:
        try:
            source = target_path.read_text()
            tree = ast.parse(source, filename=str(target_path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            return None

    lines = len(source.splitlines())
    sloc = sum(1 for line in source.splitlines() if line.strip())

    # Extract exports (top-level definitions)
    exports = _extract_exports(tree)
    classes = sum(1 for e in exports if e.kind == "class")
    functions = sum(1 for e in exports if e.kind in ("function", "async function"))

    # Type coverage for this file
    typed, total = _count_typed(tree)
    typed_pct = (typed / total * 100) if total else 0

    # Count internal references for each export
    _count_internal_refs(tree, exports)

    # Extract imports for this file
    imports = _extract_file_imports(graph, target_path, project_root)

    # Build reverse graph for blast radius
    reverse: dict[Path, set[Path]] = {}
    for src, targets in graph.resolved_edges.items():
        for t in targets:
            reverse.setdefault(t, set()).add(src)

    # Direct importers (skip __init__.py — they're pass-throughs)
    direct = {f for f in reverse.get(target_path, set()) if f.name != "__init__.py"}
    direct_importers = len(direct)

    # Transitive importers via BFS (skip __init__.py)
    visited: set[Path] = set()
    queue = list(reverse.get(target_path, set()))  # BFS from all importers including inits
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        for dep in reverse.get(node, set()):
            if dep not in visited:
                queue.append(dep)
    # Remove inits and direct from transitive count
    transitive = {f for f in visited if f.name != "__init__.py"} - direct
    transitive_importers = len(transitive)

    # Per-name usage: who imports each exported name
    export_names = {e.name for e in exports}
    _resolve_export_usage(exports, export_names, target_path, graph, project_root)

    total_cc = sum(e.complexity for e in exports if e.kind != "variable")
    max_depth = max((e.max_depth for e in exports if e.kind != "variable"), default=0)

    return FileAnalysis(
        path=rel,
        lines=lines,
        sloc=sloc,
        classes=classes,
        functions=functions,
        typed_pct=typed_pct,
        total_complexity=total_cc,
        max_depth=max_depth,
        exports=exports,
        imports=imports,
        direct_importers=direct_importers,
        transitive_importers=transitive_importers,
    )


def analyze_all(
    project_root: Path,
    cache: "ASTCache | None" = None,
) -> list[FileAnalysis]:
    """Analyze all Python files in a project. Builds the graph once."""
    graph = build_import_graph(project_root, cache=cache, skip_defaults=False, propagate_init=False)
    results: list[FileAnalysis] = []
    for py_file in graph.files:
        rel = str(py_file.relative_to(project_root))
        result = analyze_file(project_root, rel, graph=graph, cache=cache)
        if result:
            results.append(result)
    return results


def _resolve_target(project_root: Path, target: str, files: list[Path]) -> Path | None:
    """Resolve a target string to a file path."""
    # Try as relative path
    candidate = project_root / target
    if candidate in files:
        return candidate
    # Suffix match
    matches = [f for f in files if str(f).endswith(target) or str(f).endswith(f"/{target}")]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return matches[0]
    return None


def _extract_exports(tree: ast.Module) -> list[ExportedName]:
    """Extract all top-level definitions from a module."""
    exports: list[ExportedName] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            body_lines = (node.end_lineno - node.lineno + 1) if node.end_lineno else 0
            cc = _cyclomatic_complexity(node)
            depth = _max_nesting_depth(node.body, 0)
            exports.append(ExportedName(
                name=node.name, kind=kind, line=node.lineno,
                lines=body_lines, complexity=cc, max_depth=depth,
            ))
        elif isinstance(node, ast.ClassDef):
            total_cc = 0
            max_depth = 0
            methods: list[MethodInfo] = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cc = _cyclomatic_complexity(child)
                    depth = _max_nesting_depth(child.body, 0)
                    m_lines = (child.end_lineno - child.lineno + 1) if child.end_lineno else 0
                    total_cc += cc
                    max_depth = max(max_depth, depth)
                    methods.append(MethodInfo(
                        name=child.name, line=child.lineno, lines=m_lines,
                        complexity=cc, max_depth=depth,
                        is_async=isinstance(child, ast.AsyncFunctionDef),
                    ))
            body_lines = (node.end_lineno - node.lineno + 1) if node.end_lineno else 0
            # Sort methods by CC descending
            methods.sort(key=lambda m: -m.complexity)
            exports.append(ExportedName(
                name=node.name, kind="class", line=node.lineno,
                lines=body_lines, complexity=total_cc, max_depth=max_depth,
                methods=methods,
            ))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in _assign_names(node):
                if not name.startswith("_"):
                    exports.append(ExportedName(name=name, kind="variable", line=node.lineno))
    return exports


def _cyclomatic_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Compute cyclomatic complexity of a function.

    CC = 1 + number of decision points (if, elif, for, while, except,
    with, assert, and, or, ternary IfExp).
    """
    cc = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.IfExp)):
            cc += 1
        elif isinstance(child, (ast.For, ast.AsyncFor, ast.While)):
            cc += 1
        elif isinstance(child, ast.ExceptHandler):
            cc += 1
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            cc += 1
        elif isinstance(child, ast.Assert):
            cc += 1
        elif isinstance(child, ast.BoolOp):
            # each `and`/`or` adds a branch
            cc += len(child.values) - 1
    return cc


def _max_nesting_depth(body: list[ast.stmt], current: int) -> int:
    """Find the maximum nesting depth in a block of statements."""
    max_d = current
    for node in body:
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            max_d = max(max_d, _max_nesting_depth(node.body, current + 1))
            if node.orelse:
                max_d = max(max_d, _max_nesting_depth(node.orelse, current + 1))
        elif isinstance(node, ast.Try):
            max_d = max(max_d, _max_nesting_depth(node.body, current + 1))
            for handler in node.handlers:
                max_d = max(max_d, _max_nesting_depth(handler.body, current + 1))
            if node.orelse:
                max_d = max(max_d, _max_nesting_depth(node.orelse, current + 1))
            if node.finalbody:
                max_d = max(max_d, _max_nesting_depth(node.finalbody, current + 1))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            max_d = max(max_d, _max_nesting_depth(node.body, current + 1))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Nested function — reset depth counter but track max
            max_d = max(max_d, _max_nesting_depth(node.body, current + 1))
    return max_d


def _assign_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    """Extract names from an assignment node."""
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            return [node.target.id]
        return []
    names = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _count_internal_refs(tree: ast.Module, exports: list[ExportedName]) -> None:
    """Count how many times each exported name is referenced within the file."""
    export_names = {e.name: e for e in exports}

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in export_names:
            export_names[node.id].internal_refs += 1
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            # catches things like Settings.model_config but not Settings itself
            pass

    # Subtract 1 for variable definitions — assignments produce an ast.Name
    # in the target. ClassDef/FunctionDef don't produce ast.Name for their own name.
    for exp in exports:
        if exp.kind == "variable" and exp.internal_refs > 0:
            exp.internal_refs -= 1


def _count_typed(tree: ast.Module) -> tuple[int, int]:
    """Count functions with return annotations / total functions in a file."""
    typed = 0
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            total += 1
            if node.returns:
                typed += 1
    return typed, total


def _extract_file_imports(graph, target_path: Path, project_root: Path) -> list[ImportedName]:
    """Extract structured imports for the target file."""
    edges = graph.edges.get(target_path, [])
    module_to_file = graph.module_to_file
    imports: list[ImportedName] = []

    for edge in edges:
        scope = edge.scope.value
        if not edge.names:
            # import foo.bar
            resolved_file = ""
            if edge.module in module_to_file:
                resolved_file = str(module_to_file[edge.module].relative_to(project_root))
            imports.append(ImportedName(
                name=edge.module.rsplit(".", 1)[-1] if edge.module else "",
                source_module=edge.module,
                source_file=resolved_file,
                scope=scope,
            ))
        else:
            for name in edge.names:
                submodule = f"{edge.module}.{name}" if edge.module else name
                if submodule in module_to_file:
                    resolved_file = str(module_to_file[submodule].relative_to(project_root))
                elif edge.module in module_to_file:
                    resolved_file = str(module_to_file[edge.module].relative_to(project_root))
                else:
                    resolved_file = ""
                imports.append(ImportedName(
                    name=name,
                    source_module=edge.module,
                    source_file=resolved_file,
                    scope=scope,
                ))

    return imports


def _resolve_export_usage(
    exports: list[ExportedName],
    export_names: set[str],
    target_path: Path,
    graph,
    project_root: Path,
) -> None:
    """For each export, find which files import it by name."""
    module_to_file = graph.module_to_file

    # Find all module keys that point to our target file
    target_modules = {mod for mod, path in module_to_file.items() if path == target_path}

    # Skip __init__.py files that are ancestors of the target — they're just
    # re-exporting up the package chain. Cross-package __init__.py is a real consumer.
    ancestor_inits: set[Path] = set()
    parent = target_path.parent
    while parent != project_root and parent != parent.parent:
        init = parent / "__init__.py"
        if init.exists():
            ancestor_inits.add(init)
        parent = parent.parent

    for src_path, edges in graph.edges.items():
        if src_path == target_path:
            continue
        if src_path in ancestor_inits:
            continue
        src_rel = str(src_path.relative_to(project_root))

        for edge in edges:
            if edge.scope == ImportScope.TYPE_CHECKING:
                continue
            # Check if this import targets our file
            if edge.module not in target_modules:
                continue
            # Which names does it import?
            for name in edge.names:
                if name in export_names:
                    for export in exports:
                        if export.name == name and src_rel not in export.used_by:
                            export.used_by.append(src_rel)
                elif name == "*":
                    # from foo import * — all exports are potentially used
                    for export in exports:
                        if src_rel not in export.used_by:
                            export.used_by.append(src_rel)
            # import foo (no names) — we can't tell which names are used
            if not edge.names:
                pass  # could be accessing any attribute at runtime
