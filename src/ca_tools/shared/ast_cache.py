"""Shared AST cache — parse each file once, share across all detectors."""

import ast
from pathlib import Path

from .files import collect_py_files


class ASTCache:
    """Lazily parses and caches ASTs for all Python files in a project."""

    def __init__(
        self,
        project_root: Path,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ):
        self.project_root = project_root
        self._files = collect_py_files(project_root, include, exclude)
        self._cache: dict[Path, ast.Module | None] = {}
        self._sources: dict[Path, str] = {}

    @property
    def files(self) -> list[Path]:
        return self._files

    def get_ast(self, filepath: Path) -> ast.Module | None:
        """Get the parsed AST for a file, parsing on first access."""
        if filepath not in self._cache:
            self._parse(filepath)
        return self._cache.get(filepath)

    def get_source(self, filepath: Path) -> str | None:
        """Get the source text for a file."""
        if filepath not in self._sources:
            self._parse(filepath)
        return self._sources.get(filepath)

    def parse_all(self) -> None:
        """Pre-parse all files (useful for benchmarking)."""
        for f in self._files:
            if f not in self._cache:
                self._parse(f)

    def _parse(self, filepath: Path) -> None:
        try:
            source = filepath.read_text()
            tree = ast.parse(source, filename=str(filepath))
            self._cache[filepath] = tree
            self._sources[filepath] = source
        except (SyntaxError, UnicodeDecodeError, OSError):
            self._cache[filepath] = None
            self._sources[filepath] = ""
