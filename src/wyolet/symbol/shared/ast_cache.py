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
        self.failed: list[tuple[Path, str]] = []  # (filepath, error message)

    @property
    def files(self) -> list[Path]:
        return self._files

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
