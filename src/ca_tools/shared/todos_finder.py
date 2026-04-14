"""TODO/FIXME scanner — find developer-flagged tech debt in comments."""

import re
from dataclasses import dataclass
from pathlib import Path

from ca_tools.shared.ast_cache import ASTCache

_TODO_PATTERN = re.compile(
    r"#\s*(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)",
    re.IGNORECASE,
)


@dataclass
class TodoItem:
    filepath: Path
    line: int
    tag: str  # which marker was found
    text: str  # the comment text after the marker


def detect_todos(
    project_root: Path,
    cache: ASTCache,
) -> list[TodoItem]:
    """Scan Python source files for TODO/FIXME/HACK/XXX comments."""
    items: list[TodoItem] = []

    for py_file in cache.files:
        try:
            source = py_file.read_text()
        except OSError:
            continue

        for lineno, line in enumerate(source.splitlines(), 1):
            match = _TODO_PATTERN.search(line)
            if match:
                items.append(TodoItem(
                    filepath=py_file,
                    line=lineno,
                    tag=match.group(1).upper(),
                    text=match.group(2).strip(),
                ))

    items.sort(key=lambda t: (t.tag != "FIXME", t.tag != "TODO", str(t.filepath), t.line))
    return items
