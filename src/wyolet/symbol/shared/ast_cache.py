"""Shared AST cache — parse each Python file once, share across all checkers."""

import ast
import sys
from pathlib import Path

from .files import filter_paths

# Don't cache source text for files larger than this (they're rare and eat memory)
MAX_CACHED_FILE_SIZE = 512 * 1024  # 512KB


class ASTCache:
    """Lazily parses and caches Python ASTs for files in a project.

    File discovery goes through ``Linguist.file_languages``: we ask linguist
    which files are Python and use that set, instead of globbing ``*.py``.
    Languages with no AST cache (e.g. Go in the future) get their own peer
    cache; this one is Python-only by design.

    Memory management:
    - Source text is dropped after parsing (only AST kept)
    - Files over 512KB are parsed but source is not cached
    - call clear() when done to free all ASTs
    """

    def __init__(
        self,
        project_root: Path,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        linguist=None,
    ):
        self.project_root = project_root
        self._include_patterns = tuple(include or ())
        self._exclude_patterns = tuple(exclude or ())

        if linguist is None:
            from .linguist.linguist import Linguist

            linguist = Linguist()
            linguist.classify_project(str(project_root), exclude=list(exclude or ()))
        self._linguist = linguist

        self._files: list[Path] = sorted(
            filter_paths(
                (
                    path for path, lang in linguist.file_languages.items()
                    if lang.key == "python"
                ),
                project_root=project_root,
                include=include,
                exclude=exclude,
            )
        )
        self._cache: dict[Path, ast.Module | None] = {}
        self._indexable_files: list[Path] | None = None
        self.failed: list[tuple[Path, str]] = []  # (filepath, error message)

    @property
    def files(self) -> list[Path]:
        """Python files only. Used by Python AST-shape checkers.

        Cross-language consumers (SymbolIndex) should use ``indexable_files``
        which spans every language with a registered, enabled adapter.
        """
        return self._files

    @property
    def indexable_files(self) -> list[Path]:
        """Every linguist-classified file whose language has an enabled adapter.

        Excludes nothing for include/exclude — those filters are applied on
        construction. Falls back to ``files`` if the adapter registry isn't
        reachable (defensive; shouldn't happen in normal runs).
        """
        if self._indexable_files is None:
            from wyolet.symbol.adapters import default_registry

            registry = default_registry()
            from .files import filter_paths

            all_paths = list(
                filter_paths(
                    (
                        path for path, lang in self._linguist.file_languages.items()
                        if registry.has_adapter(lang.key)
                    ),
                    project_root=self.project_root,
                    include=list(self._include_patterns) or None,
                    exclude=list(self._exclude_patterns) or None,
                )
            )
            self._indexable_files = sorted(all_paths)
        return self._indexable_files

    def language_of(self, path: Path) -> str | None:
        """Linguist's classification for this path as a canonical language key
        (e.g. ``'python'``). None if the file wasn't classified.
        """
        lang = self._linguist.file_languages.get(path)
        return lang.key if lang is not None else None

    def get_ast(self, filepath: Path) -> ast.Module | None:
        """Get the parsed AST for a file, parsing on first access."""
        if filepath not in self._cache:
            self._parse(filepath)
        return self._cache.get(filepath)

    def clear(self) -> None:
        """Free all cached ASTs to release memory."""
        self._cache.clear()

    @property
    def memory_mb(self) -> float:
        """Approximate memory usage of cached ASTs in MB."""
        total = sum(sys.getsizeof(tree) for tree in self._cache.values() if tree is not None)
        return total / (1024 * 1024)

    def _parse(self, filepath: Path) -> None:
        try:
            source = filepath.read_text()
            tree = ast.parse(source, filename=str(filepath))
            self._cache[filepath] = tree
        except SyntaxError as e:
            self._cache[filepath] = None
            self.failed.append((filepath, f"SyntaxError: {e.msg} (line {e.lineno})"))
        except UnicodeDecodeError:
            self._cache[filepath] = None
            self.failed.append((filepath, "UnicodeDecodeError: not valid text"))
        except OSError as e:
            self._cache[filepath] = None
            self.failed.append((filepath, f"OSError: {e.strerror}"))
