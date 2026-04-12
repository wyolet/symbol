from .blob import Blob
from .language import Language


class LanguageStatistics:
    def __init__(self, language: Language):
        self.name = language.name
        self.id = language.language_id
        self.type = language.type
        self.color = language.color
        self.popular = language.is_popular
        self.is_counted = language.type == "programming" or language.is_popular

        self.lines: int = 0
        self.size: int = 0
        self.files: int = 0
        self.percentage_lines: float = 0
        self.percentage_bytes: float = 0

    def add(self, blob: Blob):
        self.lines += blob.sloc
        self.size += blob.size
        self.files += 1

    def dict(self):
        return {
            "lines": self.lines,
            "size": self.size,
            "files": self.files,
            "percentage_lines": self.percentage_lines,
            "percentage_bytes": self.percentage_bytes,
            "name": self.name,
            "id": self.id,
            "popular": self.popular,
            "is_counted": self.is_counted,
            "type": self.type,
            "color": self.color,
        }
