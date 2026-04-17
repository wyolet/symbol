"""Import simulator — detects circular imports the way Python does.

Simulates Python's import machinery: tracks module loading state (NOT_LOADED →
LOADING → LOADED), follows imports in source line order, and uses the filesystem
fallback for submodule resolution — exactly like importlib does at runtime.

A cycle is only reported when Python would actually fail: a name is requested
from a partially-loaded module, and that name hasn't been defined yet (it's not
a submodule file, and its definition line hasn't been reached).
"""

import ast
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ca.symbol.shared.import_graph import ImportEdge, ImportGraph, ImportScope


class ModuleState(Enum):
    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    LOADED = "loaded"


@dataclass
class CycleInfo:
    """A real circular import that would fail at runtime."""

    chain: list[str]  # Relative paths: A → B → C → A
    trigger_line: int  # Line in the last file that causes the cycle
    failed_name: str  # The name that can't be resolved
    reason: str  # Why it fails


def simulate_imports(graph: ImportGraph, project_root: Path) -> list[CycleInfo]:
    """Simulate Python's import process to find real circular import failures.

    Walks the import graph in source-line order, tracking which modules are
    NOT_LOADED, LOADING (partially executed), or LOADED. When an import hits
    a LOADING module, checks whether the requested names are available:

    - Name is a submodule file on disk → safe (Python's filesystem fallback)
    - Name was defined before the loading line → safe (already on partial module)
    - Name hasn't been defined yet → real ImportError/AttributeError
    """
    states: dict[Path, ModuleState] = {f: ModuleState.NOT_LOADED for f in graph.files}
    loading_line: dict[Path, int] = {}  # which line the module is paused at
    loading_stack: list[Path] = []  # current import chain
    cycles: list[CycleInfo] = []
    seen_chains: set[frozenset[str]] = set()

    # Precompute: for each file, at which line is each name defined?
    name_defs: dict[Path, dict[str, int]] = {}
    for f in graph.files:
        name_defs[f] = _scan_definitions(f)

    module_to_file = graph.module_to_file

    def _load(file: Path) -> None:
        if states[file] == ModuleState.LOADED:
            return
        if states[file] == ModuleState.LOADING:
            # Already loading — cycle detected, but caller handles this
            return

        states[file] = ModuleState.LOADING
        loading_stack.append(file)

        import_edges = graph.edges.get(file, [])
        for edge in import_edges:
            if edge.scope == ImportScope.TYPE_CHECKING:
                continue

            # Resolve targets for this import edge
            targets = _resolve_edge(edge, module_to_file)

            for target in targets:
                if target not in states:
                    continue  # external module, skip

                loading_line[file] = edge.line

                if states[target] == ModuleState.NOT_LOADED:
                    _load(target)

                elif states[target] == ModuleState.LOADING:
                    # Circular! Check if names are safe
                    _check_cycle(file, target, edge, loading_stack, loading_line)

                # LOADED → already done, Python skips

        loading_stack.pop()
        states[file] = ModuleState.LOADED

    def _check_cycle(
        source: Path,
        target: Path,
        edge: ImportEdge,
        stack: list[Path],
        line_map: dict[Path, int],
    ) -> None:
        """Check if a circular import would actually fail at runtime."""
        target_paused_line = line_map.get(target, 0)
        target_defs = name_defs.get(target, {})

        if not edge.names:
            # `import foo.bar` — Python just needs the module in sys.modules.
            # Since it's LOADING, the module object exists. This is fine unless
            # code immediately accesses attributes, which we can't check statically.
            # Report as info-level — the module is partially loaded.
            pass  # Not a hard failure

        failed_names = []
        for name in edge.names:
            # Check 1: is it a submodule file? (Python's filesystem fallback)
            submodule_key = f"{edge.module}.{name}" if edge.module else name
            if submodule_key in module_to_file:
                submodule_file = module_to_file[submodule_key]
                # Submodule can be loaded independently — but only if it's
                # not ALSO in the loading stack (nested cycle)
                if states.get(submodule_file) != ModuleState.LOADING:
                    continue  # Safe — filesystem fallback works
                # If the submodule itself is loading, fall through to check defs

            # Check 2: was this name defined before the paused line?
            if name in target_defs and target_defs[name] < target_paused_line:
                continue  # Safe — name already exists on partial module

            # Check 3: is the name '*'? (from foo import * — gets whatever exists)
            if name == "*":
                continue  # Gets partial exports, usually not a crash

            failed_names.append(name)

        if not failed_names:
            return  # All names are safe

        # Build the chain from the stack
        cycle_start = stack.index(target)
        chain_paths = stack[cycle_start:] + [target]
        chain_rel = [str(p.relative_to(project_root)) for p in chain_paths]

        chain_key = frozenset(chain_rel[:-1])
        if chain_key in seen_chains:
            return
        seen_chains.add(chain_key)

        for name in failed_names:
            cycles.append(CycleInfo(
                chain=chain_rel,
                trigger_line=edge.line,
                failed_name=name,
                reason=_explain_failure(name, target, target_paused_line, target_defs),
            ))

    # Simulate loading every file (like Python importing all entry points)
    for f in graph.files:
        if states[f] == ModuleState.NOT_LOADED:
            _load(f)

    return cycles


def _resolve_edge(edge: ImportEdge, module_to_file: dict[str, Path]) -> list[Path]:
    """Resolve an import edge to target file(s), following Python's rules."""
    targets: list[Path] = []

    if not edge.names:
        # `import foo.bar` → target is the module itself
        if edge.module in module_to_file:
            targets.append(module_to_file[edge.module])
        return targets

    # `from foo import bar, baz` — check each name
    has_non_submodule = False
    for name in edge.names:
        submodule = f"{edge.module}.{name}" if edge.module else name
        if submodule in module_to_file:
            targets.append(module_to_file[submodule])
        else:
            has_non_submodule = True

    # Only target the module itself if some names aren't submodules
    if has_non_submodule and edge.module and edge.module in module_to_file:
        targets.append(module_to_file[edge.module])

    return targets


def _scan_definitions(filepath: Path) -> dict[str, int]:
    """Scan a file to find at which line each name is defined.

    Returns {name: first_definition_line}. Tracks:
    - Function/class definitions (def foo, class Bar)
    - Assignments (x = ..., x: int = ...)
    - Import statements (import x, from y import z)
    - __all__ entries
    """
    try:
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return {}

    defs: dict[str, int] = {}

    for node in ast.iter_child_nodes(tree):
        # Only top-level definitions matter for partial module state
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, node.lineno)
        elif isinstance(node, ast.ClassDef):
            defs.setdefault(node.name, node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _extract_assign_names(target):
                    defs.setdefault(name, node.lineno)
        elif isinstance(node, ast.AnnAssign) and node.target:
            for name in _extract_assign_names(node.target):
                defs.setdefault(name, node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                defs.setdefault(bound, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                defs.setdefault(bound, node.lineno)

    return defs


def _extract_assign_names(target: ast.expr) -> list[str]:
    """Extract assigned names from an assignment target."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for elt in target.elts:
            names.extend(_extract_assign_names(elt))
        return names
    return []


def _explain_failure(
    name: str,
    target: Path,
    paused_line: int,
    defs: dict[str, int],
) -> str:
    """Generate a human-readable explanation of why the import fails."""
    if name in defs:
        return f"'{name}' is defined at line {defs[name]}, but module is only loaded up to line {paused_line}"
    return f"'{name}' is not defined in {target.name} at the point of circular import"
