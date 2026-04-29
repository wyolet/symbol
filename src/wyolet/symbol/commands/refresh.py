"""`symbol refresh` — reindex changed files and clear transaction history.

The escape hatch when state drifts: index out of sync with the working
tree, or .symbol/transactions/ has accumulated noise. Default is the
cheap path (incremental refresh via mtime/git diff). ``--full`` forces a
fresh build from scratch.
"""

import json as _json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

from wyolet.symbol.shared.symbol_index import SymbolIndex, get_or_build_index


console = Console()
_TX_DIR = ".symbol/transactions"


@dataclass(frozen=True)
class RefreshResult:
    status: Literal["ok"]
    source: str  # disk | refresh | rebuild | compact | full
    symbols: int
    files: int
    transactions_cleared: int


def refresh_cmd(
    *,
    project_root: str = ".",
    full: bool = False,
    keep_transactions: bool = False,
    agent: bool = False,
    format: str = "rich",
) -> None:
    project = Path(project_root).resolve()

    if full:
        # Drop the index file so get_or_build_index does a clean build.
        idx_path = project / ".symbol" / "symbol_index.msgpack.zst"
        if idx_path.exists():
            idx_path.unlink()
        index, source = get_or_build_index(project)
        source = "full"
    else:
        index, source = get_or_build_index(project)

    cleared = 0
    if not keep_transactions:
        cleared = _clear_transactions(project)

    result = RefreshResult(
        status="ok",
        source=source,
        symbols=len(index.symbols),
        files=len(index.files),
        transactions_cleared=cleared,
    )
    _render(result, format=format, agent=agent)


def _clear_transactions(project_root: Path) -> int:
    tx_root = project_root / _TX_DIR
    if not tx_root.is_dir():
        return 0
    count = 0
    for entry in tx_root.iterdir():
        if entry.is_dir():
            try:
                shutil.rmtree(entry)
                count += 1
            except OSError:
                pass
    return count


def _render(r: RefreshResult, *, format: str, agent: bool) -> None:
    if format == "json":
        print(_json.dumps({
            "status": r.status, "source": r.source,
            "symbols": r.symbols, "files": r.files,
            "transactions_cleared": r.transactions_cleared,
        }, indent=2))
        return
    if agent:
        print(f"status: {r.status}")
        print(f"source: {r.source}")
        print(f"symbols: {r.symbols}")
        print(f"files: {r.files}")
        print(f"transactions_cleared: {r.transactions_cleared}")
        return
    console.print(
        f"[green]refreshed[/green]  [dim]{r.source}[/dim]  "
        f"{r.symbols} symbols across {r.files} files"
    )
    if r.transactions_cleared:
        console.print(f"[dim]cleared {r.transactions_cleared} transaction(s)[/dim]")
