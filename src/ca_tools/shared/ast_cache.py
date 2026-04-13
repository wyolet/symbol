"""Shared AST cache — parse each file once, share across all detectors."""

import ast
import sys
from pathlib import Path

from .files import collect_py_files

# Don't cache source text for files larger than this (they're rare and eat memory)
MAX_CACHED_FILE_SIZE = 512 * 1024  # 512KB


class ASTCache:
    """Lazily parses and caches ASTs for all Python files in a project.

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
    ):
        self.project_root = project_root
        self._files = collect_py_files(project_root, include, exclude, skip_defaults=True)
        self._cache: dict[Path, ast.Module | None] = {}
        self.failed: list[tuple[Path, str]] = []  # (filepath, error message)

    @property
    def files(self) -> list[Path]:
        return self._files

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
