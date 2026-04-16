"""Workspace — per-project-root state bundle.

Holds the things that should stay warm across operations on one project:
- ``index``: SymbolIndex (expensive to build)
- ``registry``: adapter registry reference (normally the process-global)
- ``language_cache``: path → language for files not yet in the index

In CLI each invocation builds and discards a Workspace. In MCP the server
holds one Workspace per connected project root; all tool handlers read
from the same instance, so the index stays warm and caches accumulate.

Step 2 keeps this minimal. Step 3 wires in per-file language (``file_langs``
on ``SymbolIndex``) so ``adapter_for`` can skip linguist for indexed files.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ca_tools.adapters.registry import LanguageRegistry, default_registry
from ca_tools.protocols import LanguageAdapter


@dataclass
class Workspace:
    project_root: Path
    registry: LanguageRegistry = field(default_factory=default_registry)
    index: object | None = None  # SymbolIndex — imported lazily to avoid a cycle
    language_cache: dict[str, str] = field(default_factory=dict)

    def adapter_for(self, path: Path) -> LanguageAdapter:
        """Resolve the best adapter for a file.

        Resolution order: indexed language (O(1) from SymbolIndex.file_langs)
        → per-path cache → linguist detection. Every resolution gets cached.
        """
        lang = self._language_of(path)
        adapter = self.registry.for_file(path, language=lang)
        if lang is None:
            self.language_cache[str(path)] = adapter.lang
        return adapter

    def _language_of(self, path: Path) -> str | None:
        if self.index is not None:
            try:
                rel = str(path.resolve().relative_to(self.project_root))
            except ValueError:
                rel = None
            if rel is not None:
                hit = self.index.language_of_file(rel)
                if hit is not None:
                    return hit
        return self.language_cache.get(str(path))


def build_workspace(project_root: Path) -> Workspace:
    return Workspace(project_root=project_root.resolve())
