import os
from pathlib import Path

from .blob import Blob
from .language import Language
from .language_stats import LanguageStatistics
from .strategy import (
    ExtensionStrategy,
    FilenameStrategy,
    ManpageStrategy,
    ModeLineStrategy,
    ShebangStrategy,
    XMLStrategy,
)


# Well-known extensions where the common language should always win over obscure matches.
# Maps extension → preferred language name when ambiguous.
_PREFERRED: dict[str, str] = {
    ".md": "Markdown",
    ".ts": "TypeScript",
    ".tsx": "TSX",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".json": "JSON",
    ".m": "Objective-C",
    ".pl": "Perl",
    ".r": "R",
    ".d": "D",
    ".v": "Verilog",
    ".fs": "F#",
    ".cls": "Apex",
    ".h": "C",
}


def _best_candidate(languages: list[Language], blob: Blob) -> Language:
    """Pick the best language from multiple candidates for an ambiguous file."""
    ext = blob.extname.lower()

    # Check hardcoded preferences for well-known ambiguous extensions
    if ext in _PREFERRED:
        preferred_name = _PREFERRED[ext]
        for lang in languages:
            if lang.name == preferred_name:
                return lang

    # Prefer languages with fewer total extensions (more specific to this file type)
    # then prefer programming > markup > prose > data (real code over metadata)
    type_priority = {"programming": 0, "markup": 1, "prose": 2, "data": 3}
    languages.sort(key=lambda lang: (len(lang.extensions), type_priority.get(lang.type, 99)))

    return languages[0]


class Linguist:
    strategies = [
        ModeLineStrategy(),
        FilenameStrategy(),
        ShebangStrategy(),
        ExtensionStrategy(),
        XMLStrategy(),
        ManpageStrategy(),
    ]
    statistics: dict[str, LanguageStatistics]
    file_languages: dict[Path, Language]

    def __init__(self):
        self.statistics = {}
        # Populated by detect_directory / classify_project. Single source of
        # truth for "which files exist in this project and what language each
        # one is." Consumers (ASTCache, SymbolIndex, audit) read this instead
        # of re-walking or matching extensions.
        self.file_languages = {}

    def detect(self, blob: Blob, allow_empty=False):
        if blob.symlink or blob.likely_binary or blob.binary or (not allow_empty and blob.empty):
            return None

        languages: list[Language] = []

        for strategy in self.strategies:
            candidates = strategy.call(blob, languages)

            if len(candidates) == 1:
                return candidates[0]
            elif len(candidates) > 1:
                languages = candidates

        if not languages:
            return Language.find_by_name("Unknown")

        # Prefer popular languages
        for lang in languages:
            if lang.is_popular:
                return lang
            elif lang.group in Language.popular_languages():
                return Language.find_by_name(lang.group) or lang

        # For ambiguous extensions, prefer the most commonly intended language.
        # Score by: how many extensions the language claims (fewer = more specific),
        # then by popularity of the language type for this file context.
        return _best_candidate(languages, blob)

    def detect_directory(
        self,
        absolute_path: str,
        exclude: list[str] | None = None,
        *,
        collect_stats: bool = True,
    ):
        """Walk a project once: classify every file, optionally tally LOC stats.

        Always populates ``self.file_languages`` (the path→Language map).
        When ``collect_stats`` is True (default), also accumulates the
        ``LanguageStatistics`` used by ``symbol loc`` and returns the
        stats dict — that path decodes every file to count lines.

        Callers that only need the classification (audit, index) should
        pass ``collect_stats=False`` to skip the line-counting work.
        """
        paths = self._get_all_file_paths(absolute_path, exclude=exclude)

        for file_path in paths:
            blob = Blob(file_path)

            if not (language := self.detect(blob)):
                continue

            self.file_languages[Path(file_path)] = language

            if not collect_stats:
                continue

            if language.language_id not in self.statistics:
                self.statistics[language.language_id] = LanguageStatistics(language)
            self.statistics[language.language_id].add(blob)

        if collect_stats:
            self.calculate_percentages()
            return self.to_dict()
        return None

    def classify_project(
        self,
        absolute_path: str,
        exclude: list[str] | None = None,
    ) -> dict[Path, Language]:
        """Walk + classify only — no LOC accounting. Returns file_languages.

        Cheap variant for callers that just need 'what language is each file?'
        without the cost of decoding every file to count lines.
        """
        self.detect_directory(absolute_path, exclude=exclude, collect_stats=False)
        return self.file_languages

    def calculate_percentages(self):
        counted_langs = [lang for lang in self.statistics.values() if lang.is_counted]
        total_lines = sum(lang.lines for lang in counted_langs)
        total_size = sum(lang.size for lang in counted_langs)

        for lang in counted_langs:
            lang.percentage_lines = (lang.lines / total_lines * 100) if total_lines else 0
            lang.percentage_bytes = (lang.size / total_size * 100) if total_size else 0

    def all_files(self) -> list:
        """Return all FileInfo objects across all languages, for top-N analysis."""
        from .language_stats import FileInfo

        files: list[FileInfo] = []
        for lang_stat in self.statistics.values():
            files.extend(lang_stat.file_stats)
        return files

    def to_dict(self):
        return [lang.dict() for lang in self.statistics.values()]

    @staticmethod
    def _get_all_file_paths(directory: str, exclude: list[str] | None = None) -> list[str]:
        import fnmatch

        def _exc_match(rel: str, pat: str) -> bool:
            if fnmatch.fnmatch(rel, pat):
                return True
            if pat.startswith("**/"):
                return fnmatch.fnmatch(rel, pat[3:])
            return False

        paths = []
        abs_dir = os.path.abspath(directory)
        for root, dirs, files in os.walk(abs_dir):
            rel_root = os.path.relpath(root, abs_dir)
            # Filter subdirs in-place to prevent descending
            if exclude:
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith(".")
                    and not any(
                        _exc_match(os.path.join(rel_root, d), pat)
                        for pat in exclude
                    )
                ]
            else:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                rel_path = os.path.join(rel_root, file)
                if exclude and any(_exc_match(rel_path, pat) for pat in exclude):
                    continue
                paths.append(os.path.abspath(os.path.join(root, file)))
        return paths
