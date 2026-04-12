import os

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

SKIP_DIRS = {"__pycache__", "venv", ".venv", "node_modules", "env", ".git", ".hg", ".svn", ".tox", ".mypy_cache"}

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

    def __init__(self):
        self.statistics = {}

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

    def detect_directory(self, absolute_path: str):
        paths = self._get_all_file_paths(absolute_path)

        for file_path in paths:
            blob = Blob(file_path)

            if not (language := self.detect(blob)):
                continue

            if language.language_id not in self.statistics:
                self.statistics[language.language_id] = LanguageStatistics(language)
            self.statistics[language.language_id].add(blob)

        self.calculate_percentages()
        return self.to_dict()

    def calculate_percentages(self):
        counted_langs = [lang for lang in self.statistics.values() if lang.is_counted]
        total_lines = sum(lang.lines for lang in counted_langs)
        total_size = sum(lang.size for lang in counted_langs)

        for lang in counted_langs:
            lang.percentage_lines = (lang.lines / total_lines * 100) if total_lines else 0
            lang.percentage_bytes = (lang.size / total_size * 100) if total_size else 0

    def to_dict(self):
        return [lang.dict() for lang in self.statistics.values()]

    @staticmethod
    def _get_all_file_paths(directory: str) -> list[str]:
        paths = []
        for root, dirs, files in os.walk(directory):
            # Skip hidden and known junk directories
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for file in files:
                paths.append(os.path.abspath(os.path.join(root, file)))
        return paths
