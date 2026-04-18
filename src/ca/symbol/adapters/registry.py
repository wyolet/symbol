"""Language adapter registry.

Maps a language name → best registered adapter. Callers hand in a file
path; registry resolves the language (via an optional caller hint or
linguist fallback) and returns a cached adapter instance.

State shape:
- ``_classes``: lang → list[(priority, AdapterClass)] sorted descending.
  Set at import time via ``register()``.
- ``_instances``: lang → adapter instance. Lazy; constructed on first
  lookup and reused forever.

The registry is a process-global singleton. In CLI it dies with the
process; in MCP it stays warm for the life of the server. Per-root
state (index, caches) lives on ``Workspace`` instead.
"""

from pathlib import Path

from ca.symbol.protocols import LanguageAdapter


class UnsupportedLanguage(Exception):
    """Raised when no registered adapter handles a file's language."""


class LanguageRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, list[tuple[int, type[LanguageAdapter]]]] = {}
        self._instances: dict[str, LanguageAdapter] = {}

    def register(
        self,
        language: str,
        cls: type[LanguageAdapter],
        *,
        priority: int = 0,
    ) -> None:
        """Register an adapter class for a language. Higher priority wins.

        Priorities let a future semantic Python adapter (pyright-backed)
        slot above the tier-1 ``PythonAstAdapter`` without changing callers.
        """
        lang = language.lower()
        bucket = self._classes.setdefault(lang, [])
        bucket.append((priority, cls))
        bucket.sort(key=lambda pair: pair[0], reverse=True)

    def for_language(self, language: str) -> LanguageAdapter:
        lang = language.lower()
        inst = self._instances.get(lang)
        if inst is not None:
            return inst
        bucket = self._classes.get(lang)
        if not bucket:
            raise UnsupportedLanguage(f"no adapter registered for language {lang!r}")
        cls = bucket[0][1]
        inst = cls()
        self._instances[lang] = inst
        return inst

    def for_file(self, path: Path, *, language: str | None = None) -> LanguageAdapter:
        """Resolve an adapter by file path.

        If ``language`` is supplied (e.g. from ``SymbolIndex.language_of(path)``),
        it's used directly. Otherwise linguist detects from the file contents.
        """
        if language is None:
            language = _detect_language(path)
            if language is None:
                raise UnsupportedLanguage(f"could not detect language for {path}")
        return self.for_language(language)


_default = LanguageRegistry()


def default_registry() -> LanguageRegistry:
    return _default


def _detect_language(path: Path) -> str | None:
    """Run linguist on a single file; return lowercased language name or None."""
    from ca.symbol.shared.linguist.blob import Blob
    from ca.symbol.shared.linguist.linguist import Linguist

    blob = Blob(str(path))
    lang = Linguist().detect(blob)
    if lang is None or lang.name == "Unknown":
        return None
    return lang.name.lower()


# --- built-in registrations -------------------------------------------------
#
# Adapters self-register here rather than in their own module to keep the
# import graph acyclic (registry imports are cheap; adapters are heavy).

def _register_builtins() -> None:
    from ca.symbol.adapters.python_ast import PythonAstAdapter

    _default.register("python", PythonAstAdapter, priority=0)


_register_builtins()
