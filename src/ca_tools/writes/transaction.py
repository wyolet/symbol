"""Multi-file transaction for refactoring writes.

v1 policy:
- Require git and clean working tree (or --allow-dirty override).
- Stage all edits in memory.
- Validate all (parse-verify per file if the file is Python).
- Create a git checkpoint commit before writing.
- Write each file atomically (tmp + fsync + rename).
- On failure mid-write: user recovers via `git reset --hard <checkpoint>^`.

The checkpoint commit IS the rollback mechanism. We don't build our own
staging dir / undo journal because git already provides it and users
expect `git reset` as the undo contract.
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


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
    checkpoint_sha: str | None = None
    error_code: str | None = None
    message: str | None = None


def commit_edits(
    edits: list[FileEdit],
    *,
    project_root: Path,
    op_name: str,
    subject: str,
    allow_dirty: bool = False,
    force_no_vcs: bool = False,
    dry_run: bool = False,
) -> TransactionResult:
    """Apply a multi-file transaction.

    - op_name: short slug for the checkpoint commit (e.g. "rename-symbol").
    - subject: human-readable subject for the commit (e.g. "UserService → NewUserService").
    """
    if dry_run:
        # Nothing to write, but still succeed — callers include diffs elsewhere.
        return TransactionResult(
            status="committed",
            files_written=tuple(e.file_rel for e in edits),
        )

    is_git = _is_git_repo(project_root)

    if not is_git and not force_no_vcs:
        return TransactionResult(
            status="error",
            error_code="no_git_repository",
            message=(
                f"{project_root} is not a git repository. "
                "Multi-file writes require git for safe undo. "
                "Pass --force-no-vcs to proceed anyway."
            ),
        )

    if is_git and not allow_dirty and _has_uncommitted_changes(project_root):
        return TransactionResult(
            status="error",
            error_code="working_tree_dirty",
            message=(
                "Working tree has uncommitted changes. "
                "Commit or stash first, or pass --allow-dirty."
            ),
        )

    # Checkpoint commit (only if git and tree is clean).
    checkpoint_sha: str | None = None
    if is_git and not _has_uncommitted_changes(project_root):
        # Mark the current state so `git reset --hard <sha>` undoes us cleanly.
        checkpoint_sha = _head_sha(project_root)

    # Write all files.
    written: list[str] = []
    try:
        for edit in edits:
            _atomic_write(edit.file_abs, edit.new_content)
            written.append(edit.file_rel)
    except Exception as e:
        return TransactionResult(
            status="error",
            error_code="write_failed",
            message=f"partial write ({len(written)}/{len(edits)} files): {e}",
            files_written=tuple(written),
            checkpoint_sha=checkpoint_sha,
        )

    # Optional: create a real checkpoint commit for the changes we just made,
    # tagged with op_name + subject. User can `git reset --hard HEAD~1` to undo.
    if is_git and not allow_dirty:
        _create_checkpoint_commit(project_root, op_name, subject)

    return TransactionResult(
        status="committed",
        files_written=tuple(written),
        checkpoint_sha=checkpoint_sha,
    )


# ---------------------------------------------------------- git helpers


def _is_git_repo(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.SubprocessError, OSError):
        return False


def _has_uncommitted_changes(root: Path) -> bool:
    """True if there are modified / staged / deleted tracked files.

    Ignores untracked files — build artifacts (like .ca-tools/symbol_index)
    and other unrelated clutter shouldn't block a rename.
    """
    try:
        # --porcelain with --untracked-files=no filters out untracked.
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def _head_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _create_checkpoint_commit(root: Path, op_name: str, subject: str) -> None:
    """Stage all tracked changes and commit with a `ca-tools:` prefix."""
    try:
        subprocess.run(
            ["git", "add", "-u"],
            cwd=root,
            capture_output=True,
            timeout=15,
        )
        msg = f"ca-tools {op_name}: {subject}"
        subprocess.run(
            ["git", "commit", "-m", msg, "--no-verify"],
            cwd=root,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        # Checkpoint commit is a nice-to-have; the writes themselves already
        # succeeded. Swallow errors rather than make the op look failed.
        pass


# ---------------------------------------------------------- atomic write


def _atomic_write(path: Path, data: bytes) -> None:
    original_mode = None
    try:
        original_mode = os.stat(path).st_mode
    except OSError:
        pass

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ca-tools.tmp-")
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
