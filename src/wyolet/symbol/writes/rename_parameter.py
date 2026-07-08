"""`symbol rename-parameter` — rename a function/method parameter.

A parameter is *signature surface*: renaming it updates (a) the def
signature slot, (b) in-scope body uses, and (c) keyword arguments at call
sites whose callee statically resolves to the function. Positional call
arguments are never touched. The op is addressed by (function qpath, param
name) — there is no parameter index row.

This module is a thin CLI/MCP wrapper: `resolve_rename_parameter` does the
cheap index-level checks (function exists / bare identifiers) and hands off
a request; `apply_rename_parameter` delegates to the neutral
`SymbolRenamer.rename_parameter`, which owns the AST-level validation
(param exists, no collision) and the per-file rewrite + transaction.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from wyolet.symbol.shared.symbol_index import SymbolIndex
from wyolet.symbol.writes.rename_symbol import RenameSymbolResult, public_result_from_engine

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


@dataclass(frozen=True)
class RenameParameterRequest:
    func_qpath: str
    param: str
    new_name: str


def resolve_rename_parameter(
    index: SymbolIndex,
    func_qpath: str,
    param: str,
    new_name: str,
    project_root: Path,
) -> RenameParameterRequest | RenameSymbolResult:
    if not index._built:
        index.build()

    if not _IDENT_RE.match(new_name):
        return RenameSymbolResult(
            status="error", qualified_path=func_qpath,
            error_code="invalid_argument",
            message="new-name must be a bare identifier (letters, digits, underscores).",
        )
    if not _IDENT_RE.match(param):
        return RenameSymbolResult(
            status="error", qualified_path=func_qpath,
            error_code="invalid_argument",
            message="parameter must be a bare identifier.",
        )

    rows = list(index.by_path.get(func_qpath, []))
    if not rows:
        return RenameSymbolResult(
            status="error", qualified_path=func_qpath,
            error_code="symbol_not_found",
            message=f"no function at {func_qpath!r}",
        )
    if len(rows) > 1:
        return RenameSymbolResult(
            status="error", qualified_path=func_qpath,
            error_code="symbol_ambiguous",
            message=f"{len(rows)} symbols match {func_qpath!r}",
            candidates=tuple(
                f"{index.file_of(r)}:{index.range_of(r)[0]}-{index.range_of(r)[1]}"
                for r in rows
            ),
        )

    return RenameParameterRequest(func_qpath=func_qpath, param=param, new_name=new_name)


def apply_rename_parameter(
    request: RenameParameterRequest,
    *,
    project_root: Path,
    dry_run: bool = False,
    _index: SymbolIndex | None = None,
) -> RenameSymbolResult:
    from wyolet.symbol.adapters.registry import default_registry
    from wyolet.symbol.shared.symbol_index import get_or_build_index
    from wyolet.symbol.writes.rename import SymbolRenamer

    index = _index
    if index is None:
        index, _ = get_or_build_index(project_root)

    renamer = SymbolRenamer(index, project_root, default_registry())
    r = renamer.rename_parameter(
        request.func_qpath, request.param, request.new_name, dry_run=dry_run,
    )
    return public_result_from_engine(r)
