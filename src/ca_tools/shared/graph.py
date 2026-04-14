"""Import map analysis — cycles, hotspots, blast radius, fragile modules."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

from ca_tools.shared.import_graph import ImportGraph, build_import_graph
from ca_tools.shared.findings import Severity
from ca_tools.shared.project_config import MapThresholds, MetricThreshold

from ca_tools.shared.simulator import CycleInfo, simulate_imports


@dataclass
class Cycle:
    """A real circular import that would fail at runtime."""

    path: list[str]
    severity: Severity = Severity.INFO
    failed_name: str = ""
    reason: str = ""
    trigger_line: int = 0


@dataclass
class Hotspot:
    """A module imported by many others (high fan-in)."""

    module: str
    fan_in: int
    severity: Severity = Severity.INFO


@dataclass
class Fragile:
    """A module that imports many others (high fan-out)."""

    module: str
    fan_out: int
    severity: Severity = Severity.INFO


MAX_LEAF_LINES = 100  # Leaves under this size are worth inlining


@dataclass
class Leaf:
    """A small module imported by only one other module — candidate for inlining."""

    module: str
    used_by: str
    lines: int = 0


@dataclass
class DeepChain:
    """An import chain that is too deep."""

    chain: list[str]
    severity: Severity = Severity.INFO


@dataclass
class ModuleCoupling:
    """Dependency between two packages."""

    source: str  # e.g. "api"
    target: str  # e.g. "services"
    mutual: bool = False  # bidirectional = tightly coupled


@dataclass
class CouplingNode:
    """A package in the coupling tree with its deps and children."""

    name: str  # e.g. "api"
    deps: list[str] = field(default_factory=list)  # outgoing deps (full paths)
    mutual: list[str] = field(default_factory=list)  # mutual deps (full paths)
    children: list["CouplingNode"] = field(default_factory=list)


@dataclass
class MapResult:
    cycles: list[Cycle] = field(default_factory=list)
    hotspots: list[Hotspot] = field(default_factory=list)
    fragile: list[Fragile] = field(default_factory=list)
    leaves: list[Leaf] = field(default_factory=list)
    deep_chains: list[DeepChain] = field(default_factory=list)
    coupling: list[CouplingNode] = field(default_factory=list)
    total_files: int = 0
    total_edges: int = 0


def analyze_map(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    thresholds: MapThresholds | None = None,
    coupling_depth: int = 1,
    cache: "ASTCache | None" = None,
) -> MapResult:
    """Analyze the import graph for architectural insights."""
    if thresholds is None:
        thresholds = MapThresholds()

    graph = build_import_graph(project_root, include, exclude, cache=cache, skip_defaults=False, propagate_init=False)
    result = MapResult()
    result.total_files = len(graph.files)
    result.total_edges = sum(len(targets) for targets in graph.resolved_edges.values())

    rel_edges = _relativize(graph, project_root)

    # Circular imports — simulate Python's import machinery for accuracy
    cycle_infos = simulate_imports(graph, project_root)
    result.cycles = [
        Cycle(
            path=ci.chain,
            severity=thresholds.cycles.classify(len(ci.chain) - 1),
            failed_name=ci.failed_name,
            reason=ci.reason,
            trigger_line=ci.trigger_line,
        )
        for ci in cycle_infos
    ]

    result.hotspots = _find_hotspots(rel_edges, thresholds.hotspots)
    result.fragile = _find_fragile(rel_edges, thresholds.fragile, project_root)
    result.leaves = _find_leaves(rel_edges, project_root)
    result.deep_chains = _find_deep_chains(rel_edges, thresholds.deep_chains)
    result.coupling = _find_coupling(rel_edges, max_depth=coupling_depth)

    return result


@dataclass
class BlastResult:
    """Blast radius for a single module."""

    target: str
    direct: list[str] = field(default_factory=list)
    transitive: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(set(self.direct + self.transitive))


def analyze_blast(
    project_root: Path,
    target_file: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> BlastResult:
    """Find everything that depends on a given file (direct + transitive)."""
    graph = build_import_graph(project_root, include, exclude, skip_defaults=False, propagate_init=False)
    edges = _relativize(graph, project_root)

    # Build reverse graph: module → set of modules that import it
    reverse: dict[str, set[str]] = {}
    for src, targets in edges.items():
        for target in targets:
            reverse.setdefault(target, set()).add(src)

    # Find target — support partial matching
    matched = _match_target(target_file, list(edges.keys()))
    if not matched:
        return BlastResult(target=target_file)

    # Direct dependents
    direct = sorted(reverse.get(matched, set()))

    # Transitive dependents via BFS
    visited: set[str] = set()
    queue = list(reverse.get(matched, set()))
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        for dep in reverse.get(node, set()):
            if dep not in visited:
                queue.append(dep)

    transitive = sorted(visited - set(direct))

    return BlastResult(target=matched, direct=direct, transitive=transitive)


def _match_target(target: str, all_files: list[str]) -> str | None:
    """Match a target file, supporting partial paths like 'models.py' or 'src/models'."""
    # Exact match
    if target in all_files:
        return target
    # Suffix match
    matches = [f for f in all_files if f.endswith(target) or f.endswith(f"/{target}")]
    if len(matches) == 1:
        return matches[0]
    # Contains match
    matches = [f for f in all_files if target in f]
    if len(matches) == 1:
        return matches[0]
    return matches[0] if matches else None


def _is_init(path: str) -> bool:
    """Check if a path is an __init__.py file."""
    return path.endswith("__init__.py")


def _init_has_code(filepath: Path) -> bool:
    """Check if an __init__.py has real code (not just re-exports).

    Returns False if the file only contains imports, __all__, assignments,
    and docstrings. Returns True if it has function/class definitions or
    other real statements.
    """
    try:
        tree = ast.parse(filepath.read_text(), filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return True  # assume it has code if we can't parse

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Assign):
            # __all__ = [...] is re-export boilerplate
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            continue  # docstrings
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return True  # real code
        # Any other statement (for, while, if, with, etc.) counts as real code
        if not isinstance(node, ast.AnnAssign):
            return True

    return False


def _same_package(a: str, b: str) -> bool:
    """Check if two paths are in the same package (same parent directory)."""
    return a.rsplit("/", 1)[0] == b.rsplit("/", 1)[0] if "/" in a and "/" in b else False


def _relativize(graph: ImportGraph, project_root: Path) -> dict[str, set[str]]:
    """Convert absolute Path edges to relative string edges."""
    edges: dict[str, set[str]] = {}
    for src, targets in graph.resolved_edges.items():
        src_rel = str(src.relative_to(project_root))
        # Remove self-references
        edges[src_rel] = {str(t.relative_to(project_root)) for t in targets if t != src}
    # Ensure all files exist as keys even if they import nothing
    for f in graph.files:
        rel = str(f.relative_to(project_root))
        edges.setdefault(rel, set())
    return edges



def _find_hotspots(edges: dict[str, set[str]], threshold: MetricThreshold) -> list[Hotspot]:
    """Find modules with high fan-in (imported by many).

    __init__.py files are facades — resolve through them to report
    the actual modules that contain the code.
    """
    min_fan_in = threshold.info
    fan_in: dict[str, int] = {}
    for targets in edges.values():
        for target in targets:
            fan_in[target] = fan_in.get(target, 0) + 1

    # For __init__.py hotspots, redistribute their fan-in to the modules they re-export.
    # If __init__.py imports A, B, C from its package, those are the real hotspots.
    resolved: dict[str, int] = {}
    for mod, count in fan_in.items():
        if _is_init(mod) and count >= min_fan_in:
            # Find submodules that __init__.py imports (same package)
            submodules = [t for t in edges.get(mod, set()) if _same_package(mod, t)]
            if submodules:
                # Distribute the fan-in to submodules
                for sub in submodules:
                    resolved[sub] = resolved.get(sub, fan_in.get(sub, 0)) + count
                continue
        resolved[mod] = resolved.get(mod, 0) + count

    hotspots = [
        Hotspot(module=mod, fan_in=count, severity=threshold.classify(count))
        for mod, count in resolved.items()
        if count >= min_fan_in and not _is_init(mod)
    ]
    hotspots.sort(key=lambda h: h.fan_in, reverse=True)
    return hotspots


def _find_fragile(edges: dict[str, set[str]], threshold: MetricThreshold, project_root: Path) -> list[Fragile]:
    """Find modules with high fan-out (imports too many things).

    Skips __init__.py files that are pure re-exports (no real code).
    """
    min_fan_out = threshold.info
    fragile: list[Fragile] = []
    for mod, targets in edges.items():
        if len(targets) < min_fan_out:
            continue
        if _is_init(mod) and not _init_has_code(project_root / mod):
            continue
        fragile.append(Fragile(module=mod, fan_out=len(targets), severity=threshold.classify(len(targets))))
    fragile.sort(key=lambda f: f.fan_out, reverse=True)
    return fragile


def _find_leaves(edges: dict[str, set[str]], project_root: Path) -> list[Leaf]:
    """Find small modules imported by exactly one other — candidates for inlining."""
    fan_in: dict[str, list[str]] = {}
    for src, targets in edges.items():
        for target in targets:
            fan_in.setdefault(target, []).append(src)

    leaves: list[Leaf] = []
    for mod, importers in fan_in.items():
        if len(importers) != 1:
            continue
        if _is_init(mod):
            continue
        filepath = project_root / mod
        try:
            lines = len(filepath.read_text().splitlines())
        except OSError:
            continue
        if lines >= MAX_LEAF_LINES:
            continue
        leaves.append(Leaf(module=mod, used_by=importers[0], lines=lines))

    leaves.sort(key=lambda leaf: leaf.lines)
    return leaves


def _find_deep_chains(edges: dict[str, set[str]], threshold: MetricThreshold) -> list[DeepChain]:
    """Find deep import chains, excluding __init__.py from depth count.

    __init__.py files are namespace pass-throughs, not real architectural layers.
    The chain is built on the full graph but depth is measured by real modules only.
    """
    min_depth = threshold.info
    memo: dict[str, list[str]] = {}
    visiting: set[str] = set()

    def dfs(node: str) -> list[str]:
        if node in memo:
            return memo[node]
        if node in visiting:
            return []
        visiting.add(node)

        best: list[str] = []
        for target in edges.get(node, set()):
            chain = dfs(target)
            if len(chain) > len(best):
                best = chain

        visiting.discard(node)
        result = [node, *best]
        memo[node] = result
        return result

    # Compute longest chain from every node
    all_chains: list[list[str]] = []
    for node in edges:
        chain = dfs(node)
        # Filter out __init__.py — they're pass-throughs, not real depth
        real_chain = [n for n in chain if not _is_init(n)]
        if len(real_chain) >= min_depth:
            all_chains.append(real_chain)

    # Deduplicate — drop chains that are a suffix of a longer chain
    all_chains.sort(key=len, reverse=True)
    seen_starts: set[str] = set()
    unique: list[DeepChain] = []
    for chain in all_chains:
        if chain[0] not in seen_starts:
            seen_starts.add(chain[0])
            unique.append(DeepChain(chain=chain, severity=threshold.classify(len(chain))))

    return unique


def _get_package(path: str, depth: int = 1) -> str | None:
    """Extract the package at a given depth from a relative path.

    depth=1: src/api/routes/auth.py → api
    depth=2: src/api/routes/auth.py → api/routes
    depth=1: main.py → None (top-level file, no package)
    """
    parts = path.split("/")
    # Strip src/ prefix if present
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    # Need at least depth+1 parts (package segments + filename)
    if len(parts) <= depth:
        return None
    return "/".join(parts[:depth])


def _build_pkg_edges(edges: dict[str, set[str]], depth: int) -> dict[str, set[str]]:
    """Build package-level edge map at a given depth."""
    pkg_edges: dict[str, set[str]] = {}
    for src, targets in edges.items():
        src_pkg = _get_package(src, depth)
        if not src_pkg:
            continue
        for target in targets:
            tgt_pkg = _get_package(target, depth)
            if not tgt_pkg or tgt_pkg == src_pkg:
                continue
            pkg_edges.setdefault(src_pkg, set()).add(tgt_pkg)
    return pkg_edges


def _find_coupling(edges: dict[str, set[str]], max_depth: int = 1) -> list[CouplingNode]:
    """Build a coupling tree — top-level packages with nested sub-package coupling.

    Each top-level package shows its external deps, then its children show
    their own external deps (full paths on the right side).
    """
    # Depth 1: top-level packages
    top_edges = _build_pkg_edges(edges, 1)

    # Detect mutual at top level
    top_mutual: set[frozenset[str]] = set()
    for src, targets in top_edges.items():
        for tgt in targets:
            if tgt in top_edges and src in top_edges[tgt]:
                top_mutual.add(frozenset([src, tgt]))

    # Depth 2: sub-packages (only external deps — crossing top-level boundary)
    sub_edges = _build_pkg_edges(edges, 2) if max_depth >= 2 else {}

    nodes: list[CouplingNode] = []
    for pkg in sorted(top_edges):
        mutual_list = sorted(
            next(iter(pair - {pkg})) for pair in top_mutual if pkg in pair
        )
        deps_list = sorted(top_edges[pkg] - {m for m in mutual_list})

        # Build children: sub-packages under this top-level package
        children: list[CouplingNode] = []
        if max_depth >= 2:
            for sub_pkg in sorted(sub_edges):
                # Only children of this top-level package
                if not sub_pkg.startswith(pkg + "/"):
                    continue
                # Only external deps (targets outside this top-level package)
                external = sorted(
                    t for t in sub_edges[sub_pkg]
                    if not t.startswith(pkg + "/")
                )
                if external:
                    children.append(CouplingNode(name=sub_pkg, deps=external))

        nodes.append(CouplingNode(
            name=pkg,
            deps=deps_list,
            mutual=mutual_list,
            children=children,
        ))

    return nodes
