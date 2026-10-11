"""Normalize blank-line runs above def/class headers to PEP 8 spacing.

Run after a write splice to clean up the spacing around the change. AST-aware
so we don't insert blanks between a decorator and its def, or detach a leading
comment block from the def it documents. Files that fail to parse are left
untouched.
"""

import ast
import os
from pathlib import Path


def normalize_file_blank_gaps(file_abs: Path) -> bool:
    """Re-read the file, normalize blank gaps, atomically rewrite if changed.

    Returns True if the file was modified. Used as a post-write step by the
    symbol-level edit tools so byte-range splices don't leave PEP 8-violating
    spacing around the change.
    """
    source = file_abs.read_bytes()
    new_source = normalize_blank_gaps(source)
    if new_source == source:
        return False
    tmp = file_abs.with_name(file_abs.name + ".normtmp")
    tmp.write_bytes(new_source)
    os.replace(tmp, file_abs)
    return True


def normalize_blank_gaps(source: bytes) -> bytes:
    """Clamp the blank-line run above each def/class to PEP 8 spacing.

    Module-level def/class headers get 2 blank lines above; class-body methods
    get 1. Comment blocks directly preceding a header are treated as part of
    the header — the blank-line run is normalized *above* the comments.
    """
    text = source.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return source

    lines = text.splitlines(keepends=True)
    edits: list[tuple[int, int, int]] = []

    def walk(stmts: list[ast.stmt], scope: str) -> None:
        for i, node in enumerate(stmts):
            if i > 0 and _is_def_or_class(node):
                desired = 2 if scope == "module" else 1
                true_start = _true_start_line(node, lines)
                blank_top, blank_bottom = _blank_run_above(true_start, lines)
                current = (blank_bottom - blank_top + 1) if blank_top else 0
                if current != desired:
                    if blank_top:
                        edits.append((blank_top, blank_bottom, desired))
                    else:
                        edits.append((true_start, true_start - 1, desired))
            if isinstance(node, ast.ClassDef):
                walk(node.body, "class")

    walk(tree.body, "module")

    if not edits:
        return source

    new_lines = list(lines)
    for start, end, desired in sorted(edits, key=lambda e: -e[0]):
        new_blanks = ["\n"] * desired
        if end >= start:
            new_lines[start - 1 : end] = new_blanks
        else:
            new_lines[start - 1 : start - 1] = new_blanks

    return "".join(new_lines).encode("utf-8")


def _is_def_or_class(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))


def _true_start_line(node: ast.AST, lines: list[str]) -> int:
    """First line of the header block: earliest of (decorators, leading comments, def)."""
    start = node.lineno
    decs = getattr(node, "decorator_list", None) or []
    if decs:
        start = min(start, *(d.lineno for d in decs))
    while start > 1:
        prev = lines[start - 2].lstrip()
        if prev.startswith("#"):
            start -= 1
            continue
        break
    return start


def _blank_run_above(line: int, lines: list[str]) -> tuple[int | None, int | None]:
    """Find the contiguous blank-line run immediately above `line` (1-indexed).

    Returns (top, bottom) line numbers of the run, or (None, None) if no blanks.
    """
    ln = line - 1
    top: int | None = None
    bottom: int | None = None
    while 1 <= ln <= len(lines) and not lines[ln - 1].strip():
        if bottom is None:
            bottom = ln
        top = ln
        ln -= 1
    return top, bottom
