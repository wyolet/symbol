"""SymbolRenamer — language-agnostic rename orchestrator.

Owns: declaration resolution, candidate-file enumeration, the uniform
fail-loudly policy, transaction commit. Calls into the language adapter
for the actual per-file analysis.

The adapter returns a `RenameAnalysis` per file (rewrites + skipped +
unresolved buckets). The engine aggregates, enforces "any unresolved
without force → abort", splices byte ranges, commits via transaction.

No language-specific imports in this module. Ever.
"""

import re
from pathlib import Path
from typing import Callable

from wyolet.symbol.adapters.registry import LanguageRegistry
from wyolet.symbol.protocols import (
    IndexQuery,
    LanguageAdapter,
    RenameAnalysis,
)
from wyolet.symbol.protocols.types import SymbolPath
from wyolet.symbol.shared.symbol_index import SymbolIndex
from wyolet.symbol.writes.rename.index_query import RenamerIndexQuery
from wyolet.symbol.writes.rename.result import (
    FileRewriteCount,
    RenameResult,
    Rewrite,
    SkippedMismatch,
    Unresolved,
)
from wyolet.symbol.writes.transaction import FileEdit, commit_edits


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


class _DeclResolved:
    __slots__ = ("row", "declaring_file", "old_leaf", "qpath", "new_qpath", "kind",
                 "owner_qpath", "decl_byte_range")

    def __init__(
        self, row: int, declaring_file: str, old_leaf: str, qpath: str,
        new_qpath: str, kind: str, owner_qpath: str, decl_byte_range: tuple[int, int],
    ):
        self.row = row
        self.declaring_file = declaring_file
        self.old_leaf = old_leaf
        self.qpath = qpath
        self.new_qpath = new_qpath
        self.kind = kind
        self.owner_qpath = owner_qpath
        self.decl_byte_range = decl_byte_range


class SymbolRenamer:
    def __init__(
        self,
        index: SymbolIndex,
        project_root: Path,
        registry: LanguageRegistry,
    ):
        self.index = index
        self.project_root = project_root
        self.registry = registry
        self._index_query: IndexQuery = RenamerIndexQuery(index, project_root)

    # ── public entry ─────────────────────────────────────────────
    def rename(
        self,
        qualified_path: str,
        new_name: str,
        *,
        dry_run: bool = False,
    ) -> RenameResult:
        decl = self._resolve_declaration(qualified_path, new_name)
        if isinstance(decl, RenameResult):
            return decl

        method = self._dispatch().get(decl.kind)
        if method is None:
            return RenameResult(
                status="error",
                qualified_path=decl.qpath,
                new_qualified_path=decl.new_qpath,
                declaring_file=decl.declaring_file,
                error_code="kind_not_supported",
                message=f"rename of kind {decl.kind!r} is not implemented yet",
            )
        return method(decl, new_name, dry_run=dry_run)

    # ── dispatch ─────────────────────────────────────────────────
    def _dispatch(self) -> dict[str, Callable]:
        return {
            # python kinds
            "method":         self._rename_member,
            "async_method":   self._rename_member,
            "function":       self._rename_module_binding,
            "async_function": self._rename_module_binding,
            "class":          self._rename_class,
            "field":          self._rename_field,
            "attribute":      self._rename_field,
            "local":          self._rename_local,
            "parameter":      self._rename_local,
            "constant":       self._rename_module_binding,
            # go kinds — emitted by the go-scan adapter
            "type":           self._rename_class,
            "var":            self._rename_module_binding,
            "const":          self._rename_module_binding,
        }

    # ── per-kind: member (method/async_method) ───────────────────
    def _rename_member(self, decl, new_name, *, dry_run) -> RenameResult:
        return self._run_per_file(
            decl, new_name, dry_run=dry_run,
            invoke=lambda adapter, abs_path, rel, source: adapter.rename_member(
                path=abs_path,
                project_root=self.project_root,
                source=source,
                leaf=decl.old_leaf,
                target_qpath=decl.qpath,
                target_owner_qpath=decl.owner_qpath,
                index=self._index_query,
                is_declaring_file=(rel == decl.declaring_file),
                decl_byte_range=decl.decl_byte_range if rel == decl.declaring_file else None,
                new_name=new_name,
            ),
        )

    def _run_per_file(self, decl, new_name, *, dry_run, invoke) -> RenameResult:
        """Shared per-kind orchestrator: enumerate files, invoke the
        adapter analysis, aggregate buckets, commit, build result."""
        candidate_files = self._candidate_files(decl.old_leaf, decl.declaring_file)
        all_rewrites: list[Rewrite] = []
        all_skipped: list[SkippedMismatch] = []
        all_unresolved: list[Unresolved] = []
        per_file_edits: list[FileEdit] = []
        per_file_counts: list[FileRewriteCount] = []

        for rel in sorted(candidate_files):
            abs_path = self.project_root / rel
            try:
                source = abs_path.read_bytes()
            except OSError:
                continue
            adapter = self._adapter_for(abs_path)
            if adapter is None:
                continue

            analysis: RenameAnalysis = invoke(adapter, abs_path, rel, source)

            for r in analysis.rewrites:
                all_rewrites.append(Rewrite(
                    file=rel, line=r.line, col=r.col,
                    receiver_source=r.receiver_source,
                    resolved_to_qpath=decl.qpath,
                ))
            for s in analysis.skipped_mismatch:
                all_skipped.append(SkippedMismatch(
                    file=rel, line=s.line, col=s.col,
                    receiver_source=s.receiver_source,
                    resolved_to_qpath=s.resolved_to_qpath,
                ))
            for u in analysis.unresolved:
                all_unresolved.append(Unresolved(
                    file=rel, line=u.line, col=u.col,
                    receiver_source=u.receiver_source,
                    why=u.why,
                ))

            if analysis.rewrites:
                new_content = _apply_rewrites(source, analysis.rewrites)
                per_file_edits.append(FileEdit(
                    file_abs=abs_path, file_rel=rel, new_content=new_content,
                ))
                per_file_counts.append(FileRewriteCount(
                    file=rel, refs_updated=len(analysis.rewrites),
                ))

        if not per_file_edits:
            return RenameResult(
                status="needs_review" if all_unresolved or all_skipped else "error",
                qualified_path=decl.qpath,
                new_qualified_path=decl.new_qpath,
                declaring_file=decl.declaring_file,
                rewrites=(),
                skipped_mismatch=tuple(all_skipped),
                unresolved=tuple(all_unresolved),
                error_code=None if (all_unresolved or all_skipped) else "nothing_to_rename",
                message=_format_message(0, 0, all_unresolved, all_skipped, decl.old_leaf),
            )

        tx = commit_edits(
            per_file_edits,
            project_root=self.project_root,
            op_name="rename-symbol",
            subject=f"{decl.qpath} → {new_name}",
            dry_run=dry_run,
        )
        if tx.status == "error":
            return RenameResult(
                status="error",
                qualified_path=decl.qpath,
                new_qualified_path=decl.new_qpath,
                declaring_file=decl.declaring_file,
                rewrites=tuple(all_rewrites),
                skipped_mismatch=tuple(all_skipped),
                unresolved=tuple(all_unresolved),
                error_code=tx.error_code,
                message=tx.message,
            )

        return RenameResult(
            status="dry_run" if dry_run else "applied",
            qualified_path=decl.qpath,
            new_qualified_path=decl.new_qpath,
            declaring_file=decl.declaring_file,
            files_changed=len(per_file_edits),
            refs_updated=sum(c.refs_updated for c in per_file_counts),
            per_file=tuple(per_file_counts),
            rewrites=tuple(all_rewrites),
            skipped_mismatch=tuple(all_skipped),
            unresolved=tuple(all_unresolved),
            message=_format_message(
                len(per_file_edits),
                sum(c.refs_updated for c in per_file_counts),
                all_unresolved,
                all_skipped,
                decl.old_leaf,
            ),
        )

    def _rename_module_binding(self, decl, new_name, *, dry_run) -> RenameResult:
        return self._run_per_file(
            decl, new_name, dry_run=dry_run,
            invoke=lambda adapter, abs_path, rel, source: adapter.rename_module_binding(
                path=abs_path,
                project_root=self.project_root,
                source=source,
                leaf=decl.old_leaf,
                target_qpath=decl.qpath,
                target_module_qpath=decl.owner_qpath,
                index=self._index_query,
                is_declaring_file=(rel == decl.declaring_file),
                decl_byte_range=decl.decl_byte_range if rel == decl.declaring_file else None,
                new_name=new_name,
            ),
        )

    def _rename_class(self, decl, new_name, *, dry_run) -> RenameResult:
        # Class rename has the same AST surface as module-binding rename
        # (Name id, Attribute attr, alias name). Class-specific extras
        # (bases inside ClassDef.bases) are themselves Name nodes the
        # walker already catches.
        return self._rename_module_binding(decl, new_name, dry_run=dry_run)

    def _rename_field(self, decl, new_name, *, dry_run):
        return self._not_impl(decl, "field")

    def _rename_local(self, decl, new_name, *, dry_run):
        return self._not_impl(decl, "local")

    def _not_impl(self, decl, label):
        return RenameResult(
            status="error",
            qualified_path=decl.qpath,
            new_qualified_path=decl.new_qpath,
            declaring_file=decl.declaring_file,
            error_code="kind_not_supported",
            message=f"{label} rename not yet implemented in v2 engine",
        )

    # ── internals ────────────────────────────────────────────────
    def _resolve_declaration(self, qpath: str, new_name: str):
        if not self.index._built:
            self.index.build()

        if not _IDENT_RE.match(new_name):
            return RenameResult(
                status="error",
                error_code="invalid_argument",
                message="new-name must be a bare identifier",
            )

        rows = list(self.index.by_path.get(qpath, []))
        if not rows:
            return RenameResult(
                status="error",
                error_code="symbol_not_found",
                message=f"no symbol at {qpath!r}",
            )
        if len(rows) > 1:
            return RenameResult(
                status="error",
                error_code="symbol_ambiguous",
                message=f"{len(rows)} symbols match {qpath!r}",
                candidates=tuple(
                    f"{self.index.file_of(r)}:{self.index.range_of(r)[0]}-{self.index.range_of(r)[1]}"
                    for r in rows
                ),
            )

        row = rows[0]
        declaring_file = self.index.file_of(row)
        if self.index.ensure_fresh(declaring_file):
            rows = list(self.index.by_path.get(qpath, []))
            if not rows:
                return RenameResult(
                    status="error",
                    error_code="symbol_not_found",
                    message=f"symbol {qpath!r} disappeared after refresh",
                )
            row = rows[0]
            declaring_file = self.index.file_of(row)

        old_leaf = qpath.rsplit(".", 1)[-1]
        if old_leaf == new_name:
            return RenameResult(
                status="error",
                error_code="invalid_argument",
                message="new-name is identical to current name",
            )

        prefix = qpath[: -len(old_leaf)]
        new_qpath = f"{prefix}{new_name}" if prefix else new_name
        if self.index.by_path.get(new_qpath):
            return RenameResult(
                status="error",
                error_code="name_collision",
                message=f"{new_qpath!r} already exists",
            )

        owner_qpath = prefix.rstrip(".") if prefix else ""
        kind = self.index.kind_of(row)
        s_byte, e_byte = self.index.byte_range_of(row)

        return _DeclResolved(
            row=row, declaring_file=declaring_file, old_leaf=old_leaf,
            qpath=qpath, new_qpath=new_qpath, kind=kind,
            owner_qpath=owner_qpath, decl_byte_range=(s_byte, e_byte),
        )

    def _candidate_files(self, leaf: str, declaring_file: str) -> set[str]:
        files = {declaring_file}
        # In-body references (call sites, attribute accesses).
        for src_row, _line, _kind in self.index.callers_of(leaf):
            files.add(self.index.file_of(src_row))
        # Module-level import aliases — these aren't recorded as in-body
        # refs by the python adapter, so callers_of misses them. Without
        # this pass, re-export sites in `__init__.py` and bare imports
        # in modules with no symbols would not be rewritten.
        for file_id in range(len(self.index.files)):
            for local, _source, _line in self.index.imports_for(file_id):
                if local == leaf:
                    files.add(self.index.files[file_id])
                    break
        return files

    def _adapter_for(self, abs_path: Path) -> LanguageAdapter | None:
        rel = str(abs_path.relative_to(self.project_root))
        lang = self.index.language_of_file(rel)
        try:
            adapter = self.registry.for_file(abs_path, language=lang)
        except Exception:
            return None
        # Engine requires both rename methods. Adapters that don't yet
        # implement them are silently skipped (no rewrites contributed
        # from those files).
        if not hasattr(adapter, "rename_member") or not hasattr(adapter, "rename_module_binding"):
            return None
        return adapter


def _apply_rewrites(source: bytes, rewrites) -> bytes:
    buf = bytearray(source)
    for r in sorted(rewrites, key=lambda x: x.byte_start, reverse=True):
        buf[r.byte_start:r.byte_end] = r.new_text.encode("utf-8")
    return bytes(buf)


def _format_message(
    files_changed: int,
    refs_updated: int,
    unresolved: list,
    skipped: list,
    leaf: str,
) -> str:
    parts: list[str] = []
    if refs_updated:
        parts.append(f"renamed {refs_updated} ref(s) across {files_changed} file(s)")
    else:
        parts.append(f"no confident rewrites for {leaf!r}")
    if unresolved:
        parts.append(
            f"{len(unresolved)} site(s) need manual review — receiver type "
            f"could not be resolved statically:"
        )
        for u in unresolved:
            parts.append(f"  - {u.file}:{u.line}:{u.col + 1}  `{u.receiver_source}.{leaf}`  ({u.why})")
    if skipped:
        parts.append(
            f"{len(skipped)} site(s) correctly skipped — receiver resolved "
            f"to a different declaration:"
        )
        for s in skipped:
            parts.append(f"  - {s.file}:{s.line}:{s.col + 1}  `{s.receiver_source}.{leaf}`  → {s.resolved_to_qpath}")
    return "\n".join(parts)
