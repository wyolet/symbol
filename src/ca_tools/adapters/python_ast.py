"""Python language adapter backed by the stdlib `ast` module.

v1 scope: implements LanguageAdapter (syntactic). SemanticLanguageAdapter
methods come in a follow-up — they'll reuse the module-level analysis
already living in the symbol index and checkers.

Cache strategy: keyed on (path, hash(source)). Parsing is fast enough that
content-hash keying avoids mtime-based invalidation complexity.
"""

import ast
import builtins as _builtins
import hashlib
from pathlib import Path

from ca_tools.protocols import (
    LanguageAdapter,
    ParseResult,
    RawImport,
    RawRef,
    RawSymbol,
)

_BUILTINS: frozenset[str] = frozenset(dir(_builtins))
_SCOPE_BOUNDARY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_SYMBOL_KINDS: dict[type, str] = {
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "async_function",
    ast.ClassDef: "class",
}


class PythonAstAdapter(LanguageAdapter):
    """Tier-1 Python adapter. Handmade tier-2 capability lands later."""

    lang = "python"

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], ast.Module] = {}

    # ---------------------------------------------------------- protocol

    def symbols(self, path: Path, source: bytes) -> list[RawSymbol]:
        tree = self._parse(path, source)
        line_offsets = _line_offsets(source)
        return [
            self._symbol_from(child, path_prefix=_module_name(path), line_offsets=line_offsets)
            for child in ast.iter_child_nodes(tree)
            if type(child) in _SYMBOL_KINDS
        ]

    def imports(self, path: Path, source: bytes) -> list[RawImport]:
        tree = self._parse(path, source)
        line_offsets = _line_offsets(source)
        out: list[RawImport] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                statement = _slice(source, node, line_offsets)
                names = tuple(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
                byte_range = _node_byte_range(node, line_offsets)
                out.append(
                    RawImport(
                        line=node.lineno,
                        byte_range=byte_range,
                        statement=statement,
                        imported_names=names,
                        module=None,
                    )
                )
            elif isinstance(node, ast.ImportFrom):
                statement = _slice(source, node, line_offsets)
                names = tuple(alias.asname or alias.name for alias in node.names)
                byte_range = _node_byte_range(node, line_offsets)
                out.append(
                    RawImport(
                        line=node.lineno,
                        byte_range=byte_range,
                        statement=statement,
                        imported_names=names,
                        module=node.module,
                    )
                )

        return out

    def references_in(
        self, path: Path, source: bytes, symbol: RawSymbol
    ) -> list[RawRef]:
        """Name references inside a symbol's body, skipping locals and builtins.

        Returns the same agent-relevant name dependencies that the symbol
        index's refs table holds — names the symbol depends on from outside
        its own body, plus attribute accesses on `self`/module.
        """
        tree = self._parse(path, source)
        node = _find_node_by_range(tree, symbol.byte_range, _line_offsets(source))
        if node is None:
            return []

        locals_ = _collect_local_names(node)
        line_offsets = _line_offsets(source)
        out: list[RawRef] = []
        seen: set[tuple[str, int]] = set()

        for sub in _walk_own(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                name = sub.id
                if name in locals_ or name in _BUILTINS:
                    continue
                key = (name, sub.lineno)
                if key in seen:
                    continue
                seen.add(key)
                byte_offset = _line_col_to_byte(sub.lineno, sub.col_offset, line_offsets)
                out.append(RawRef(name=name, line=sub.lineno, byte_offset=byte_offset))

        return out

    def validate_syntax(self, source: bytes) -> ParseResult:
        try:
            ast.parse(source)
            return ParseResult(ok=True)
        except SyntaxError as e:
            return ParseResult(
                ok=False,
                error_line=e.lineno,
                error_message=e.msg,
            )

    def preview(self, body: str, signature: str, max_lines: int = 3) -> str:
        """First few meaningful code lines after the signature.

        Skips leading docstrings and comment-only lines. Preserves indentation
        relative to the first picked line. Python-specific: triple-quote
        detection and ``#`` comments live here, not in the reads layer.
        """
        lines = body.splitlines()

        joined = ""
        consumed = 0
        for i, line in enumerate(lines):
            joined = (joined + " " + line.strip()).strip()
            if joined.replace(" ", "").endswith(signature.replace(" ", "")):
                consumed = i + 1
                break
        body_lines = lines[consumed:]

        i = 0
        while i < len(body_lines) and not body_lines[i].strip():
            i += 1
        if i < len(body_lines):
            stripped = body_lines[i].strip()
            for quote in ('"""', "'''", '"', "'"):
                if stripped.startswith(quote):
                    if stripped.count(quote) >= 2 and len(stripped) > len(quote):
                        i += 1
                    else:
                        i += 1
                        while i < len(body_lines) and quote not in body_lines[i]:
                            i += 1
                        i += 1
                    break

        start = None
        for j, line in enumerate(body_lines[i:], start=i):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            start = j
            break
        if start is None:
            return ""

        picked = body_lines[start : start + max_lines]
        base_indent = len(picked[0]) - len(picked[0].lstrip())
        return "\n".join(
            line[base_indent:] if len(line) >= base_indent else line for line in picked
        )

    def invalidate(self, path: Path) -> None:
        key_prefix = str(path)
        self._cache = {k: v for k, v in self._cache.items() if k[0] != key_prefix}

    # ---------------------------------------------------------- internals

    def _parse(self, path: Path, source: bytes) -> ast.Module:
        key = (str(path), hashlib.sha1(source).hexdigest())
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        tree = ast.parse(source, filename=str(path))
        self._cache[key] = tree
        return tree

    def _symbol_from(
        self,
        node: ast.AST,
        *,
        path_prefix: str,
        line_offsets: list[int],
    ) -> RawSymbol:
        kind = _SYMBOL_KINDS[type(node)]
        name = node.name  # type: ignore[attr-defined]
        qpath = f"{path_prefix}.{name}" if path_prefix else name

        start_line = node.lineno  # type: ignore[attr-defined]
        end_line = getattr(node, "end_lineno", start_line) or start_line
        # Start at col 0 so leading indentation is part of the symbol's byte range.
        start_byte = _line_col_to_byte(start_line, 0, line_offsets)
        end_byte = _line_col_to_byte(
            end_line, getattr(node, "end_col_offset", 0) or 0, line_offsets
        )

        children: list[RawSymbol] = []
        if isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if type(child) in _SYMBOL_KINDS:
                    kind_name = _SYMBOL_KINDS[type(child)]
                    # Methods keep "method" kind instead of "function".
                    if kind_name == "function":
                        children.append(
                            self._method_from(child, path_prefix=qpath, line_offsets=line_offsets)
                        )
                    else:
                        children.append(
                            self._symbol_from(child, path_prefix=qpath, line_offsets=line_offsets)
                        )

        return RawSymbol(
            kind=kind,
            name=name,
            qualified_path=qpath,
            byte_range=(start_byte, end_byte),
            line_range=(start_line, end_line),
            signature_line=start_line,
            children=tuple(children),
        )

    def _method_from(
        self,
        node: ast.AST,
        *,
        path_prefix: str,
        line_offsets: list[int],
    ) -> RawSymbol:
        sym = self._symbol_from(node, path_prefix=path_prefix, line_offsets=line_offsets)
        kind = "async_method" if isinstance(node, ast.AsyncFunctionDef) else "method"
        return RawSymbol(
            kind=kind,
            name=sym.name,
            qualified_path=sym.qualified_path,
            byte_range=sym.byte_range,
            line_range=sym.line_range,
            signature_line=sym.signature_line,
            children=sym.children,
        )


# ---------------------------------------------------------- helpers


def _line_offsets(source: bytes) -> list[int]:
    """Byte offsets of the start of each line (1-indexed into the returned list)."""
    offsets = [0, 0]  # offsets[0] unused; offsets[1] = 0 (line 1 starts at byte 0)
    for i, b in enumerate(source):
        if b == 0x0A:  # \n
            offsets.append(i + 1)
    return offsets


def _line_col_to_byte(line: int, col: int, line_offsets: list[int]) -> int:
    if line >= len(line_offsets):
        return line_offsets[-1]
    return line_offsets[line] + col


def _node_byte_range(node: ast.AST, line_offsets: list[int]) -> tuple[int, int]:
    start = _line_col_to_byte(node.lineno, node.col_offset, line_offsets)  # type: ignore[attr-defined]
    end_line = getattr(node, "end_lineno", node.lineno) or node.lineno  # type: ignore[attr-defined]
    end_col = getattr(node, "end_col_offset", 0) or 0
    end = _line_col_to_byte(end_line, end_col, line_offsets)
    return (start, end)


def _slice(source: bytes, node: ast.AST, line_offsets: list[int]) -> str:
    start, end = _node_byte_range(node, line_offsets)
    return source[start:end].decode("utf-8", errors="replace")


def _module_name(path: Path) -> str:
    """Best-effort module name from a path (no project-root awareness yet)."""
    return path.stem


def _walk_own(node: ast.AST):
    """Descendants of `node` not crossing into nested def/class/lambda."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, _SCOPE_BOUNDARY):
            stack.extend(ast.iter_child_nodes(n))


def _extract_bound_names(target: ast.AST):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _extract_bound_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _extract_bound_names(target.value)


def _collect_local_names(node: ast.AST) -> set[str]:
    locals_: set[str] = set()

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        for a in (*args.args, *args.posonlyargs, *args.kwonlyargs):
            locals_.add(a.arg)
        if args.vararg:
            locals_.add(args.vararg.arg)
        if args.kwarg:
            locals_.add(args.kwarg.arg)

    for sub in _walk_own(node):
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                locals_.update(_extract_bound_names(tgt))
        elif isinstance(sub, ast.AnnAssign):
            locals_.update(_extract_bound_names(sub.target))
        elif isinstance(sub, ast.AugAssign):
            locals_.update(_extract_bound_names(sub.target))
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            locals_.update(_extract_bound_names(sub.target))
        elif isinstance(sub, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in sub.generators:
                locals_.update(_extract_bound_names(gen.target))
        elif isinstance(sub, ast.withitem) and sub.optional_vars is not None:
            locals_.update(_extract_bound_names(sub.optional_vars))
        elif isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
            locals_.add(sub.target.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            locals_.add(sub.name)

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            locals_.add(child.name)

    return locals_


def _find_node_by_range(
    tree: ast.Module, byte_range: tuple[int, int], line_offsets: list[int]
) -> ast.AST | None:
    """Find the top-level (or class-nested) symbol whose byte range matches."""
    target_start, target_end = byte_range
    for node in ast.walk(tree):
        if type(node) not in _SYMBOL_KINDS:
            continue
        nr = _node_byte_range(node, line_offsets)
        if nr == (target_start, target_end):
            return node
    return None
