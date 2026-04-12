"""Import graph construction and orphan file detection."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

from ca_tools.shared.files import collect_py_files


@dataclass
class ImportGraph:
    files: list[Path] = field(default_factory=list)
    edges: dict[Path, set[str]] = field(default_factory=dict)
    resolved_edges: dict[Path, set[Path]] = field(default_factory=dict)


@dataclass
class OrphanFile:
    filepath: Path
    reason: str


def build_import_graph(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> ImportGraph:
    graph = ImportGraph()
    py_files = collect_py_files(project_root, include, exclude)
    graph.files = py_files

    module_to_file = _build_module_map(project_root, py_files)

    for py_file in py_files:
        try:
            source = py_file.read_text()
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        imports = _extract_imports(tree)
        graph.edges[py_file] = imports

        resolved: set[Path] = set()
        for imp in imports:
            if imp in module_to_file:
                resolved.add(module_to_file[imp])
            parts = imp.rsplit(".", 1)
            if len(parts) == 2 and parts[0] in module_to_file:
                resolved.add(module_to_file[parts[0]])
        graph.resolved_edges[py_file] = resolved

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

            if module_parts[0] == "src" and len(module_parts) > 1:
                alt_name = ".".join(module_parts[1:])
                module_to_file[alt_name] = py_file

    return module_to_file


def _extract_imports(tree: ast.Module) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


def detect_orphans(project_root: Path, graph: ImportGraph | None = None) -> list[OrphanFile]:
    if graph is None:
        graph = build_import_graph(project_root)

    imported_files: set[Path] = set()
    for targets in graph.resolved_edges.values():
        imported_files.update(targets)

    orphans: list[OrphanFile] = []
    for py_file in graph.files:
        if py_file in imported_files:
            continue

        rel = py_file.relative_to(project_root)
        name = rel.name

        if name == "__init__.py":
            continue
        if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
            continue

        rel_str = str(rel)
        if "script" in rel_str or "scripts" in rel_str:
            reason = "likely one-off script"
        elif "test" in rel_str or "tests" in rel_str:
            reason = "likely test file"
        elif name in ("manage.py", "setup.py"):
            reason = "project tool"
        else:
            reason = "likely dead code"

        orphans.append(OrphanFile(filepath=py_file, reason=reason))

    return orphans


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
