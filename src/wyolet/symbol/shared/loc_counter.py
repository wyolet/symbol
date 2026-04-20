"""Lines of code counting — scc-style breakdown by language."""

from dataclasses import dataclass, field
from pathlib import Path

LANGUAGES: dict[str, tuple[str, list[str], tuple[str, str] | None]] = {
    ".py": ("Python", ["#"], ('"""', '"""')),
    ".js": ("JavaScript", ["//"], ("/*", "*/")),
    ".ts": ("TypeScript", ["//"], ("/*", "*/")),
    ".tsx": ("TypeScript JSX", ["//"], ("/*", "*/")),
    ".jsx": ("JavaScript JSX", ["//"], ("/*", "*/")),
    ".go": ("Go", ["//"], ("/*", "*/")),
    ".rs": ("Rust", ["//"], ("/*", "*/")),
    ".java": ("Java", ["//"], ("/*", "*/")),
    ".c": ("C", ["//"], ("/*", "*/")),
    ".h": ("C Header", ["//"], ("/*", "*/")),
    ".cpp": ("C++", ["//"], ("/*", "*/")),
    ".cs": ("C#", ["//"], ("/*", "*/")),
    ".rb": ("Ruby", ["#"], ("=begin", "=end")),
    ".php": ("PHP", ["//", "#"], ("/*", "*/")),
    ".swift": ("Swift", ["//"], ("/*", "*/")),
    ".kt": ("Kotlin", ["//"], ("/*", "*/")),
    ".scala": ("Scala", ["//"], ("/*", "*/")),
    ".sh": ("Shell", ["#"], None),
    ".bash": ("Bash", ["#"], None),
    ".zsh": ("Zsh", ["#"], None),
    ".sql": ("SQL", ["--"], ("/*", "*/")),
    ".html": ("HTML", [], ("<!--", "-->")),
    ".css": ("CSS", [], ("/*", "*/")),
    ".scss": ("SCSS", ["//"], ("/*", "*/")),
    ".yml": ("YAML", ["#"], None),
    ".yaml": ("YAML", ["#"], None),
    ".toml": ("TOML", ["#"], None),
    ".json": ("JSON", [], None),
    ".xml": ("XML", [], ("<!--", "-->")),
    ".md": ("Markdown", [], None),
    ".txt": ("Text", [], None),
    ".dockerfile": ("Dockerfile", ["#"], None),
    ".r": ("R", ["#"], None),
    ".lua": ("Lua", ["--"], ("--[[", "]]")),
    ".ex": ("Elixir", ["#"], None),
    ".exs": ("Elixir Script", ["#"], None),
    ".erl": ("Erlang", ["%"], None),
    ".hs": ("Haskell", ["--"], ("{-", "-}")),
    ".tf": ("Terraform", ["#"], ("/*", "*/")),
    ".proto": ("Protobuf", ["//"], ("/*", "*/")),
    ".graphql": ("GraphQL", ["#"], None),
    ".vue": ("Vue", ["//"], ("<!--", "-->")),
    ".svelte": ("Svelte", ["//"], ("<!--", "-->")),
}

NAMED_FILES: dict[str, str] = {
    "Dockerfile": "Dockerfile",
    "Makefile": "Makefile",
    "Justfile": "Justfile",
    "Vagrantfile": "Ruby",
    "Procfile": "Procfile",
    "Gemfile": "Ruby",
    "Rakefile": "Ruby",
    ".gitignore": "Git Config",
    ".dockerignore": "Docker Config",
    ".env": "Env",
    ".env.example": "Env",
}

SKIP_DIRS = {"__pycache__", "venv", ".venv", "node_modules", "env", ".git", ".hg", ".svn", ".tox", ".mypy_cache"}


@dataclass
class FileStats:
    lines: int = 0
    code: int = 0
    blanks: int = 0
    comments: int = 0


@dataclass
class LangStats:
    language: str
    files: int = 0
    lines: int = 0
    code: int = 0
    blanks: int = 0
    comments: int = 0

    @property
    def code_pct(self) -> float:
        return (self.code / self.lines * 100) if self.lines else 0.0


@dataclass
class LocResult:
    by_language: dict[str, LangStats] = field(default_factory=dict)
    total_files: int = 0
    total_lines: int = 0
    total_code: int = 0
    total_blanks: int = 0
    total_comments: int = 0


def count_loc(project_root: Path) -> LocResult:
    """Count lines of code across the project, grouped by language."""
    result = LocResult()

    for filepath in sorted(project_root.rglob("*")):
        if not filepath.is_file():
            continue

        rel = filepath.relative_to(project_root)
        parts = rel.parts
        if any(p.startswith(".") or p in SKIP_DIRS for p in parts[:-1]):
            continue

        lang = _detect_language(filepath)
        if lang is None:
            continue

        try:
            text = filepath.read_text(errors="replace")
        except OSError:
            continue

        stats = _count_file(text, filepath.suffix.lower())

        if lang not in result.by_language:
            result.by_language[lang] = LangStats(language=lang)

        ls = result.by_language[lang]
        ls.files += 1
        ls.lines += stats.lines
        ls.code += stats.code
        ls.blanks += stats.blanks
        ls.comments += stats.comments

        result.total_files += 1
        result.total_lines += stats.lines
        result.total_code += stats.code
        result.total_blanks += stats.blanks
        result.total_comments += stats.comments

    return result


def _detect_language(filepath: Path) -> str | None:
    name = filepath.name

    if name in NAMED_FILES:
        return NAMED_FILES[name]

    if name.lower().startswith("dockerfile"):
        return "Dockerfile"

    ext = filepath.suffix.lower()
    if ext in LANGUAGES:
        return LANGUAGES[ext][0]

    return None


def _count_file(text: str, ext: str) -> FileStats:
    stats = FileStats()
    lang_info = LANGUAGES.get(ext)
    line_prefixes = lang_info[1] if lang_info else []
    block = lang_info[2] if lang_info else None

    in_block = False
    lines = text.splitlines()
    stats.lines = len(lines)

    for line in lines:
        stripped = line.strip()

        if not stripped:
            stats.blanks += 1
            continue

        if in_block:
            stats.comments += 1
            if block and block[1] in stripped:
                in_block = False
            continue

        if block and stripped.startswith(block[0]):
            stats.comments += 1
            if block[1] not in stripped[len(block[0]) :]:
                in_block = True
            continue

        if any(stripped.startswith(p) for p in line_prefixes):
            stats.comments += 1
            continue

        stats.code += 1

    return stats
