"""Undo the most recent successful symbol-write transaction.

Reads the latest manifest under ``.symbol/transactions/``, restores each
file from its pre-image (atomic write), then renames the transaction
directory to ``<id>.undone`` so it doesn't get picked up again.

Files that were created (no pre-image recorded) are deleted on undo.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wyolet.symbol.writes.transaction import _atomic_write


_TX_DIR = ".symbol/transactions"
_MANIFEST = "manifest.json"


@dataclass(frozen=True)
class UndoResult:
    status: Literal["undone", "nothing_to_undo", "error"]
    transaction_id: str | None = None
    op: str | None = None
    subject: str | None = None
    files_restored: tuple[str, ...] = ()
    files_skipped: tuple[str, ...] = ()
    error_code: str | None = None
    message: str | None = None


def undo_last(project_root: Path) -> UndoResult:
    """Roll back the most recent transaction.

    No-op (returns ``nothing_to_undo``) when no usable transaction exists.
    """
    tx_root = project_root / _TX_DIR
    if not tx_root.is_dir():
        return UndoResult(status="nothing_to_undo")

    candidates = sorted(
        (p for p in tx_root.iterdir() if p.is_dir() and not p.name.endswith(".undone")),
        key=lambda p: p.name,
        reverse=True,
    )
    if not candidates:
        return UndoResult(status="nothing_to_undo")

    tx_dir = candidates[0]
    manifest_path = tx_dir / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return UndoResult(
            status="error",
            transaction_id=tx_dir.name,
            error_code="manifest_unreadable",
            message=str(e),
        )

    restored: list[str] = []
    skipped: list[str] = []
    for entry in manifest.get("files", []):
        file_rel = entry.get("file_rel")
        pre_blob = entry.get("pre_image")
        if not file_rel:
            continue
        target = (project_root / file_rel).resolve()
        try:
            if pre_blob is None:
                # File was created by the op; undo means delete.
                target.unlink(missing_ok=True)
            else:
                pre_bytes = (tx_dir / pre_blob).read_bytes()
                _atomic_write(target, pre_bytes)
            restored.append(file_rel)
        except OSError:
            skipped.append(file_rel)

    # Mark this transaction undone so the next invocation picks the prior op.
    try:
        tx_dir.rename(tx_dir.with_name(tx_dir.name + ".undone"))
    except OSError:
        pass

    return UndoResult(
        status="undone",
        transaction_id=manifest.get("id"),
        op=manifest.get("op"),
        subject=manifest.get("subject"),
        files_restored=tuple(restored),
        files_skipped=tuple(skipped),
    )
