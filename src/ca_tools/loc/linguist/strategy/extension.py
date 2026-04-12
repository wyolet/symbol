from ..blob import Blob
from ..config.load import load_config
from ..language import Language
from .base import BaseStrategy


class ExtensionStrategy(BaseStrategy):
    generic_extensions: list = []

    def __init__(self):
        self.load_generic_extensions()

    def call(self, blob: Blob, candidates: list):
        if self.is_generic(blob.name):
            return candidates

        languages = Language.find_by_extension(blob.name)

        return [lang for lang in languages if lang in candidates] if candidates else languages

    def is_generic(self, filename: str) -> bool:
        filename = filename.lower()
        return any(filename.endswith(ext) for ext in self.generic_extensions)

    def load_generic_extensions(self):
        if not self.generic_extensions:
            generic = load_config("generic")
            self.generic_extensions = generic.get("extensions", [])
