from ..blob import Blob
from ..language import Language
from .base import BaseStrategy


class FilenameStrategy(BaseStrategy):
    def call(self, blob: Blob, candidates: list):
        name = blob.name or ""
        languages = _dedup(Language.find_by_filename(name))

        if candidates:
            return [lang for lang in languages if lang in candidates]
        return languages


def _dedup(langs: list[Language]) -> list[Language]:
    seen: set[int] = set()
    result: list[Language] = []
    for lang in langs:
        if lang.language_id not in seen:
            seen.add(lang.language_id)
            result.append(lang)
    return result
