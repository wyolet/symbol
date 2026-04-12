"""Import map analysis — cycles, hotspots, blast radius, fragile modules."""

from dataclasses import dataclass, field
from pathlib import Path

from ca_tools.audit.orphans import ImportGraph, build_import_graph


@dataclass
class Cycle:
    """A circular import path."""

    path: list[str]


@dataclass
class Hotspot:
    """A module imported by many others (high fan-in)."""

    module: str
    fan_in: int


@dataclass
class Fragile:
    """A module that imports many others (high fan-out)."""

    module: str
    fan_out: int


@dataclass
class Leaf:
    """A module imported by only one other module."""

    module: str
    used_by: str


@dataclass
class MapResult:
    cycles: list[Cycle] = field(default_factory=list)
    hotspots: list[Hotspot] = field(default_factory=list)
    fragile: list[Fragile] = field(default_factory=list)
    leaves: list[Leaf] = field(default_factory=list)
    deep_chains: list[list[str]] = field(default_factory=list)
    total_files: int = 0
    total_edges: int = 0


def analyze_map(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    min_fan_in: int = 5,
    min_fan_out: int = 8,
    min_chain_depth: int = 5,
) -> MapResult:
    """Analyze the import graph for architectural insights."""
    graph = build_import_graph(project_root, include, exclude)
    result = MapResult()
    result.total_files = len(graph.files)
    result.total_edges = sum(len(targets) for targets in graph.resolved_edges.values())

    rel_edges = _relativize(graph, project_root)

    result.cycles = _find_cycles(rel_edges)
    result.hotspots = _find_hotspots(rel_edges, min_fan_in)
    result.fragile = _find_fragile(rel_edges, min_fan_out)
    result.leaves = _find_leaves(rel_edges)
    result.deep_chains = _find_deep_chains(rel_edges, min_chain_depth)

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
    graph = build_import_graph(project_root, include, exclude)
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


def _same_package(a: str, b: str) -> bool:
    """Check if two paths are in the same package (same parent directory)."""
    return a.rsplit("/", 1)[0] == b.rsplit("/", 1)[0] if "/" in a and "/" in b else False


def _is_init_reexport_cycle(cycle_path: list[str]) -> bool:
    """Check if a cycle is just __init__.py ↔ submodule in the same package.

    These are normal Python package re-export patterns, not real circular deps.
    """
    nodes = cycle_path[:-1]  # last element repeats the first

    # Self-reference: __init__.py → __init__.py (noise)
    if len(nodes) == 1 and _is_init(nodes[0]):
        return True

    # Two-node cycle: __init__.py ↔ submodule in same package
    if len(nodes) == 2:
        a, b = nodes
        if (_is_init(a) or _is_init(b)) and _same_package(a, b):
            return True

    # Multi-node: all nodes in same package and one is __init__.py
    if any(_is_init(n) for n in nodes) and len({n.rsplit("/", 1)[0] for n in nodes if "/" in n}) == 1:
        return True

    return False


def _relativize(graph: ImportGraph, project_root: Path) -> dict[str, set[str]]:
    """Convert absolute Path edges to relative string edges."""
    edges: dict[str, set[str]] = {}
    for src, targets in graph.resolved_edges.items():
        src_rel = str(src.relative_to(project_root))
        edges[src_rel] = {str(t.relative_to(project_root)) for t in targets}
    # Ensure all files exist as keys even if they import nothing
    for f in graph.files:
        rel = str(f.relative_to(project_root))
        edges.setdefault(rel, set())
    return edges


def _find_cycles(edges: dict[str, set[str]]) -> list[Cycle]:
    """Find all unique circular import cycles."""
    cycles: list[Cycle] = []
    visited: set[str] = set()
    path: list[str] = []
    on_stack: set[str] = set()
    seen_cycles: set[frozenset[str]] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        on_stack.add(node)
        path.append(node)

        for neighbor in edges.get(node, set()):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in on_stack:
                # Found a cycle — extract it
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                cycle_key = frozenset(cycle_path[:-1])
                if cycle_key not in seen_cycles:
                    seen_cycles.add(cycle_key)
                    cycles.append(Cycle(path=cycle_path))

        path.pop()
        on_stack.discard(node)

    for node in edges:
        if node not in visited:
            dfs(node)

    # Filter out __init__.py re-export patterns — they're not real cycles
    return [c for c in cycles if not _is_init_reexport_cycle(c.path)]


def _find_hotspots(edges: dict[str, set[str]], min_fan_in: int) -> list[Hotspot]:
    """Find modules with high fan-in (imported by many).

    __init__.py files are facades — resolve through them to report
    the actual modules that contain the code.
    """
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
        Hotspot(module=mod, fan_in=count)
        for mod, count in resolved.items()
        if count >= min_fan_in and not _is_init(mod)
    ]
    hotspots.sort(key=lambda h: h.fan_in, reverse=True)
    return hotspots


def _find_fragile(edges: dict[str, set[str]], min_fan_out: int) -> list[Fragile]:
    """Find modules with high fan-out (imports too many things)."""
    fragile = [
        Fragile(module=mod, fan_out=len(targets)) for mod, targets in edges.items() if len(targets) >= min_fan_out
    ]
    fragile.sort(key=lambda f: f.fan_out, reverse=True)
    return fragile


def _find_leaves(edges: dict[str, set[str]]) -> list[Leaf]:
    """Find modules imported by exactly one other module."""
    fan_in: dict[str, list[str]] = {}
    for src, targets in edges.items():
        for target in targets:
            fan_in.setdefault(target, []).append(src)

    leaves = [Leaf(module=mod, used_by=importers[0]) for mod, importers in fan_in.items() if len(importers) == 1]
    leaves.sort(key=lambda leaf: leaf.module)
    return leaves


def _find_deep_chains(edges: dict[str, set[str]], min_depth: int) -> list[list[str]]:
    """Find all import chains at least min_depth deep, sorted longest first."""
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
        if len(chain) >= min_depth:
            all_chains.append(chain)

    # Deduplicate — drop chains that are a suffix of a longer chain
    all_chains.sort(key=len, reverse=True)
    seen_starts: set[str] = set()
    unique: list[list[str]] = []
    for chain in all_chains:
        if chain[0] not in seen_starts:
            seen_starts.add(chain[0])
            unique.append(chain)

    return unique
