"""Language model — lightweight replacement for pydantic model."""

from dataclasses import dataclass, field


@dataclass
class ProgrammingLanguage:
    fs_name: str | None = None
    type: str | None = None
    aliases: list[str] = field(default_factory=list)
    ace_mode: str | None = None
    codemirror_mode: str | None = None
    codemirror_mime_type: str | None = None
    wrap: bool = False
    extensions: list[str] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)
    interpreters: list[str] = field(default_factory=list)
    language_id: int | None = None
    color: str | None = None
    tm_scope: str | None = None
    group: str | None = None
