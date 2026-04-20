"""`symbol index` — build the symbol lookup table and write it to disk."""

import time
from pathlib import Path

from rich.console import Console

from wyolet.symbol.shared.context import build_context
from wyolet.symbol.shared.symbol_index import SymbolIndex

console = Console()


def index_cmd(path: str = ".") -> None:
    project_root = Path(path).resolve()
    ctx = build_context(project_root)

    t0 = time.time()
    idx = SymbolIndex(ctx.cache)
    idx.build()
    build_ms = (time.time() - t0) * 1000

    t0 = time.time()
    out = idx.save()
    save_ms = (time.time() - t0) * 1000

    size_kb = out.stat().st_size / 1024
    stats = idx.stats
    console.print(
        f"[green]indexed[/green] {stats['files']} files → "
        f"{stats['symbols']} symbols, {stats['imports']} imports, {stats['refs']} refs"
    )
    console.print(
        f"[dim]build {build_ms:.0f} ms  •  save {save_ms:.0f} ms  •  "
        f"{size_kb:.1f} KB  •  {out.relative_to(project_root)}[/dim]"
    )
