"""`symbol outline` — show a file's symbols as a parent-child tree."""

import json as _json
from pathlib import Path

from rich.console import Console
from rich.tree import Tree

from ca.symbol.reads.outline import outline as outline_query
from ca.symbol.shared.symbol_index import get_or_build_index

console = Console()

_KIND_GLYPH = {
    "class": "◆",
    "function": "ƒ",
    "async_function": "ƒ",
    "method": "•",
}


def _attach(parent: Tree, node: dict) -> None:
    glyph = _KIND_GLYPH.get(node["kind"], "·")
    sig = node.get("signature") or node["path"].rsplit(".", 1)[-1]
    # Trim trailing colon for compactness in tree display.
    display_sig = sig[:-1] if sig.endswith(":") else sig
    label = (
        f"{glyph} [cyan]{display_sig}[/cyan] "
        f"[dim]{node['start_line']}-{node['end_line']}[/dim]"
    )
    sub = parent.add(label)
    for child in node["children"]:
        _attach(sub, child)


def outline_cmd(file: str, path: str = ".", format: str = "rich") -> None:
    project_root = Path(path).resolve()
    index, _source = get_or_build_index(project_root)

    # Try to interpret as a filesystem path first (resolve it); otherwise
    # pass through as a symbol path.
    arg = file
    fs_candidate = Path(file)
    if fs_candidate.exists():
        try:
            arg = str(fs_candidate.resolve().relative_to(project_root))
        except ValueError:
            arg = file

    roots = outline_query(index, arg)

    if format == "json":
        print(_json.dumps(roots, indent=2))
        return

    if not roots:
        console.print(f"[yellow]Nothing to outline for[/yellow] {arg!r}")
        return

    tree = Tree(f"[bold]{arg}[/bold]")
    for root in roots:
        _attach(tree, root)
    console.print(tree)
