"""Import graph construction and orphan file detection."""

import ast
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import ca_tools.frameworks  # noqa: F401 — registers framework hooks
from ca_tools.shared.files import collect_py_files
from ca_tools.shared.pipeline import SKIP_ORPHAN, make_context, run_pipeline


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

        imports = _extract_imports(tree, py_file, project_root)
        graph.edges[py_file] = imports

        resolved: set[Path] = set()
        for imp in imports:
            # Direct module match
            if imp in module_to_file:
                resolved.add(module_to_file[imp])
            # Parent module match (from foo.bar import baz → foo.bar)
            parts = imp.rsplit(".", 1)
            if len(parts) == 2 and parts[0] in module_to_file:
                resolved.add(module_to_file[parts[0]])

        # Remove self-references (a file can't meaningfully import itself)
        resolved.discard(py_file)
        graph.resolved_edges[py_file] = resolved

    # Second pass: resolve "from pkg import name" where name is a submodule file
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


def _extract_imports(tree: ast.Module, filepath: Path, project_root: Path) -> set[str]:
    """Extract all imported module names, resolving relative imports."""
    imports: set[str] = set()

    # Compute this file's package for relative import resolution
    rel = filepath.relative_to(project_root)
    rel_parts = list(rel.parts)
    if rel_parts[-1] == "__init__.py":
        file_package_parts = rel_parts[:-1]
    else:
        file_package_parts = rel_parts[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                # Absolute import
                if node.module:
                    imports.add(node.module)
                    # Also add "module.name" for each imported name
                    # This catches "from pkg import submodule" where submodule is a file
                    for alias in node.names:
                        imports.add(f"{node.module}.{alias.name}")
            else:
                # Relative import: resolve based on file's package
                # level=1 means current package, level=2 means parent, etc.
                base_parts = file_package_parts[: len(file_package_parts) - (node.level - 1)]
                if node.module:
                    resolved = ".".join(base_parts + node.module.split("."))
                else:
                    resolved = ".".join(base_parts)
                imports.add(resolved)
                # Also resolve each imported name as a potential submodule
                for alias in node.names:
                    imports.add(f"{resolved}.{alias.name}")

    return imports


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


def detect_orphans(
    project_root: Path,
    graph: ImportGraph | None = None,
    entry_point_files: set[Path] | None = None,
) -> list[OrphanFile]:
    if graph is None:
        graph = build_import_graph(project_root)

    imported_files: set[Path] = set()
    for targets in graph.resolved_edges.values():
        imported_files.update(targets)

    # Collect skip patterns from framework hooks
    context = make_context(project_root)
    skip_patterns = run_pipeline(SKIP_ORPHAN, project_root, context)

    orphans: list[OrphanFile] = []
    for py_file in graph.files:
        if py_file in imported_files:
            continue

        rel = py_file.relative_to(project_root)
        name = rel.name
        rel_str = str(rel)

        # Check against skip patterns from framework hooks
        if _matches_skip(name, rel_str, skip_patterns):
            continue

        # Skip files already detected as entry points
        if entry_point_files and py_file in entry_point_files:
            continue

        # Classify the orphan
        if "script" in rel_str or "scripts" in rel_str:
            reason = "likely one-off script"
        elif "test" in rel_str or "tests" in rel_str:
            reason = "likely test file"
        else:
            reason = "likely dead code"

        orphans.append(OrphanFile(filepath=py_file, reason=reason))

    return orphans


def _matches_skip(name: str, rel_str: str, patterns: list[str]) -> bool:
    """Check if a file matches any skip pattern.

    Patterns can be:
    - Exact filenames: "conftest.py", "__init__.py"
    - Glob patterns: "test_*.py", "alembic/versions/*.py"
    """
    for pattern in patterns:
        # Exact filename match
        if name == pattern:
            return True
        # Glob against filename
        if fnmatch.fnmatch(name, pattern):
            return True
        # Glob against relative path
        if fnmatch.fnmatch(rel_str, pattern):
            return True
    return False


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
