"""TODO/FIXME scanner — find developer-flagged tech debt in comments."""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from wyolet.symbol.shared.context import AnalysisContext
from wyolet.symbol.shared.registry import register, views
from wyolet.symbol.shared.findings import Severity

I1, I2 = "  ", "    "

_TODO_PATTERN = re.compile(
    r"#\s*(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)",
    re.IGNORECASE,
)


@dataclass
class TodoItem:
    filepath: Path
    line: int
    tag: str
    text: str


@register(
    name="todos",
    description="TODO/FIXME/HACK/XXX comments in source",
    kind="file",
    default_severity=Severity.INFO,
    contributes_to_report=False,
    priority=60,
)
def detect(
    ctx: AnalysisContext,
    filepath: Path,
    tree=None,
) -> list[TodoItem]:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    results: list[TodoItem] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        match = _TODO_PATTERN.search(line)
        if match:
            results.append(TodoItem(
                filepath=filepath,
                line=lineno,
                tag=match.group(1).upper(),
                text=match.group(2).strip(),
            ))
    return results


# ── Views ────────────────────────────────────────────────────────────


def rich_view(items: list[TodoItem], ctx: AnalysisContext, console: Console) -> None:
    if not items:
        return

    # Sort: FIXME first, then TODO, then by file/line
    items = sorted(items, key=lambda t: (t.tag != "FIXME", t.tag != "TODO", str(t.filepath), t.line))

    console.print()
    tag_counts: dict[str, int] = defaultdict(int)
    for t in items:
        tag_counts[t.tag] += 1

    tag_summary = "  ".join(f"[dim]{tag}:{count}[/dim]" for tag, count in sorted(tag_counts.items()))
    console.print(Text(f"{I1}\U0001f4cc TODO/FIXME ({len(items)})", style="bold yellow"))
    console.print(f"{I2}{tag_summary}")
    console.print()

    for item in items[:10]:
        rel = item.filepath.relative_to(ctx.project_root)
        loc = f"{rel}:{item.line}"
        tag_color = "red" if item.tag == "FIXME" else "yellow"
        console.print(
            f"{I2}[{tag_color}]{item.tag}[/{tag_color}]  "
            f"[bold]{loc:<40s}[/bold] [dim]{item.text}[/dim]"
        )
    if len(items) > 10:
        console.print(f"{I2}[dim]... and {len(items) - 10} more[/dim]")


def json_view(items: list[TodoItem], ctx: AnalysisContext) -> list[dict]:
    items = sorted(items, key=lambda t: (t.tag != "FIXME", t.tag != "TODO", str(t.filepath), t.line))
    return [
        {
            "file": str(t.filepath.relative_to(ctx.project_root)),
            "line": t.line,
            "tag": t.tag,
            "text": t.text,
        }
        for t in items
    ]


views("todos", rich=rich_view, json=json_view)
