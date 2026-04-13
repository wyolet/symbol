from dataclasses import dataclass

from .blob import Blob
from .language import Language


@dataclass
class FileInfo:
    """Per-file stats collected during detection."""

    path: str
    lines: int  # total lines (loc)
    sloc: int  # source lines (non-blank)
    size: int  # bytes


class LanguageStatistics:
    def __init__(self, language: Language):
        self.name = language.name
        self.id = language.language_id
        self.type = language.type
        self.color = language.color
        self.popular = language.is_popular
        self.is_counted = language.type == "programming" or language.is_popular

        self.loc: int = 0  # total lines including blanks
        self.sloc: int = 0  # source lines (non-blank)
        self.size: int = 0
        self.files: int = 0
        self.percentage_lines: float = 0
        self.percentage_bytes: float = 0
        self.file_stats: list[FileInfo] = []

    @property
    def lines(self) -> int:
        """Alias for sloc — backwards compat with existing consumers."""
        return self.sloc

    def add(self, blob: Blob):
        self.loc += blob.loc
        self.sloc += blob.sloc
        self.size += blob.size
        self.files += 1
        self.file_stats.append(FileInfo(
            path=blob.path,
            lines=blob.loc,
            sloc=blob.sloc,
            size=blob.size,
        ))

    def dict(self):
        return {
            "loc": self.loc,
            "sloc": self.sloc,
            "lines": self.sloc,  # backwards compat
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
