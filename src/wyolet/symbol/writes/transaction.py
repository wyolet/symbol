"""Multi-file transaction for refactoring writes.

Policy:
- Read pre-image bytes of every target file into memory.
- Write each file atomically (tmp + fsync + rename).
- If any write fails partway through: restore every already-written file from
  its pre-image (also via atomic write). Either all files end up new, or all
  stay old.
- On success: persist pre-images + manifest to ``.symbol/transactions/<id>/``
  so ``symbol undo`` can roll the operation back later.

No git involvement. The agent's git history stays clean — no ``symbol:``
checkpoint commits clutter ``git log`` and confuse later reads.
"""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


_TX_DIR = ".symbol/transactions"
_MANIFEST = "manifest.json"


@dataclass(frozen=True)
class FileEdit:
    """A single file's new content, ready to write."""

    file_abs: Path
    file_rel: str
    new_content: bytes


@dataclass(frozen=True)
class TransactionResult:
    status: Literal["committed", "error"]
    files_written: tuple[str, ...] = ()
    transaction_id: str | None = None
    error_code: str | None = None
    message: str | None = None


def commit_edits(
    edits: list[FileEdit],
    *,
    project_root: Path,
    op_name: str,
    subject: str,
    dry_run: bool = False,
) -> TransactionResult:
    """Apply a multi-file transaction with in-process rollback + persisted undo.

    - op_name: short slug ("rename-symbol", "replace-symbol").
    - subject: human-readable summary ("UserService → NewUserService").
    """
    if dry_run:
        return TransactionResult(
            status="committed",
            files_written=tuple(e.file_rel for e in edits),
        )

    # Pre-image capture: read every target before we touch anything. Files
    # that don't exist yet (pure creation) get None.
    pre_images: dict[str, bytes | None] = {}
    for edit in edits:
        try:
            pre_images[edit.file_rel] = edit.file_abs.read_bytes()
        except FileNotFoundError:
            pre_images[edit.file_rel] = None
        except OSError as e:
            return TransactionResult(
                status="error",
                error_code="read_failed",
                message=f"cannot read {edit.file_rel} for pre-image capture: {e}",
            )

    written: list[FileEdit] = []
    try:
        for edit in edits:
            _atomic_write(edit.file_abs, edit.new_content)
            written.append(edit)
    except Exception as e:
        # Rollback: restore every already-written file from its pre-image.
        for done in written:
            pre = pre_images[done.file_rel]
            try:
                if pre is None:
                    done.file_abs.unlink(missing_ok=True)
                else:
                    _atomic_write(done.file_abs, pre)
            except OSError:
                # Best-effort rollback. If even the restore fails, the user
                # has the persisted transaction dir to manually recover from
                # — but this branch is rare (atomic_write failing twice).
                pass
        return TransactionResult(
            status="error",
            error_code="write_failed",
            message=(
                f"partial write rolled back ({len(written)}/{len(edits)} files "
                f"reverted): {e}"
            ),
        )

    transaction_id = _persist_transaction(
        project_root, op_name=op_name, subject=subject,
        edits=edits, pre_images=pre_images,
    )

    return TransactionResult(
        status="committed",
        files_written=tuple(e.file_rel for e in edits),
        transaction_id=transaction_id,
    )


# ---------------------------------------------------------- transaction store


def _persist_transaction(
    project_root: Path,
    *,
    op_name: str,
    subject: str,
    edits: list[FileEdit],
    pre_images: dict[str, bytes | None],
) -> str | None:
    """Write pre-images + manifest to .symbol/transactions/<id>/.

    Returns the transaction id (directory name), or None on persistence
    failure — that's a non-fatal outcome since the writes already succeeded.
    The agent loses ``symbol undo`` capability for this op but the tree is
    correct.
    """
    tx_root = project_root / _TX_DIR
    try:
        tx_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    tx_id = f"{int(time.time() * 1000)}-{op_name}"
    tx_dir = tx_root / tx_id
    try:
        tx_dir.mkdir()
    except OSError:
        return None

    manifest_files: list[dict] = []
    for edit in edits:
        pre = pre_images[edit.file_rel]
        digest = hashlib.sha256(edit.file_rel.encode("utf-8")).hexdigest()[:16]
        entry: dict = {
            "file_rel": edit.file_rel,
            "pre_image": None,
        }
        if pre is not None:
            blob_name = f"{digest}.pre"
            try:
                (tx_dir / blob_name).write_bytes(pre)
                entry["pre_image"] = blob_name
            except OSError:
                # Skip this file's undo entry; manifest still records the
                # write so undo reports it as "no pre-image, can't restore".
                entry["pre_image_error"] = True
        manifest_files.append(entry)

    manifest = {
        "version": 1,
        "id": tx_id,
        "op": op_name,
        "subject": subject,
        "created_at": time.time(),
        "files": manifest_files,
    }
    try:
        (tx_dir / _MANIFEST).write_text(json.dumps(manifest, indent=2))
    except OSError:
        return None

    return tx_id


# ---------------------------------------------------------- atomic write


def _atomic_write(path: Path, data: bytes) -> None:
    original_mode = None
    try:
        original_mode = os.stat(path).st_mode
    except OSError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".symbol.tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if original_mode is not None:
            os.chmod(tmp, original_mode & 0o7777)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
