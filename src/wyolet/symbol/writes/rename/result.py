"""Result types for AST-based rename.

Three buckets per operation:
  - rewrites:         sites we changed
  - skipped_mismatch: same-leaf refs we identified as another declaration
  - unresolved:       sites we could not resolve to any declaration

Policy: apply every confident rewrite, surface every unresolved site
with a named reason. The caller (typically an AI agent) reviews the
unresolved list and decides what to do with those specific lines.
"""

from dataclasses import dataclass, field
from typing import Literal

Status = Literal[
    "applied",        # rewrites applied (commit succeeded)
    "dry_run",        # rewrites identified, not written
    "needs_review",   # no confident rewrites; only unresolved/skipped sites surfaced
    "error",          # operation failed before classification (bad input, etc.)
]


@dataclass(frozen=True)
class Rewrite:
    file: str
    line: int
    col: int
    receiver_source: str          # "" for bare-name refs
    resolved_to_qpath: str        # declaration this ref resolved to (== target)


@dataclass(frozen=True)
class SkippedMismatch:
    file: str
    line: int
    col: int
    receiver_source: str
    resolved_to_qpath: str        # the OTHER declaration this ref binds to


@dataclass(frozen=True)
class Unresolved:
    file: str
    line: int
    col: int
    receiver_source: str
    why: str                      # human-readable failure mode


@dataclass(frozen=True)
class FileRewriteCount:
    file: str
    refs_updated: int


@dataclass(frozen=True)
class AffectedInterface:
    """A contract type the rename target implements whose method-set
    contains a method named the rename leaf — renaming will leave the
    contract unsatisfied.

    Surface-only concept: lives here next to the other render-facing
    records (Rewrite / SkippedMismatch / Unresolved) and **not** in
    protocols/types.py. The protocol contract (RenameAnalysis) doesn't
    know about it; adapters that compute it expose it via the optional
    `pop_affected_interfaces` extension method, which the renamer
    invokes only if present.

    Today only the Go adapter populates these (via go/types interface
    satisfaction). Python tier-1 doesn't compute Protocol/ABC impacts;
    adding it later would slot into the same field.
    """

    interface_qpath: str
    method_qpath: str
    file: str
    line: int


@dataclass(frozen=True)
class RenameResult:
    status: Status
    qualified_path: str = ""
    new_qualified_path: str = ""
    declaring_file: str = ""

    files_changed: int = 0
    refs_updated: int = 0
    per_file: tuple[FileRewriteCount, ...] = ()

    rewrites: tuple[Rewrite, ...] = ()
    skipped_mismatch: tuple[SkippedMismatch, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()

    # Surface-only: contract types the rename impacts. Populated by
    # adapters that compute it (Go via go/types interface satisfaction)
    # through the optional `pop_affected_interfaces` extension method.
    affected_interfaces: tuple[AffectedInterface, ...] = ()

    error_code: str | None = None
    message: str | None = None
    candidates: tuple[str, ...] = field(default_factory=tuple)
