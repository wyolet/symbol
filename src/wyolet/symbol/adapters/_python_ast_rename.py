"""Python-AST rename internals. Used only by PythonAstAdapter.

Algorithm for member rename per file:

  1. Walk the AST for `Attribute(attr==leaf)` nodes.
  2. If the leaf is owned by a single declaration globally (fast path),
     every hit is a rewrite without further checks.
  3. Otherwise (careful mode): resolve each receiver to a class qpath
     via local name analysis (self/cls, imports, same-file class defs,
     assignment scan, annotation scan). Compare to `target_owner_qpath`.
     Equal → rewrite. Different → skipped_mismatch. Unresolvable →
     unresolved (with a human-readable why).

Limitations of tier-1 (local AST only, no type checker):

  - `super().x` not yet resolved — needs `class_bases` in the index.
  - Subclass call sites resolve to the subclass's own qpath, so we can
    only equality-match against `target_owner_qpath`; cross-class
    inheritance is skipped pending the same `class_bases` work.
  - Receivers bound to call expressions (factories, returns) are
    unresolved — needs tier-2 type inference.

Each unresolved site carries a `why` string. Every new `why` seen in
practice is a candidate to extend this resolver.
"""

import ast

from wyolet.symbol.protocols.index_query import IndexQuery
from wyolet.symbol.protocols.types import (
    ByteRewrite,
    RenameAnalysis,
    SkippedMismatchSite,
    SymbolPath,
    UnresolvedSite,
)


# ──────────────────────────────────────────────────────────────────────
# byte-offset utilities

def line_byte_offsets(source: bytes) -> list[int]:
    out = [0, 0]
    for i, b in enumerate(source):
        if b == 0x0A:
            out.append(i + 1)
    return out


def _col_to_byte(source: bytes, line_starts: list[int], line: int, col: int) -> int:
    line_start = line_starts[line]
    line_end = line_starts[line + 1] if line + 1 < len(line_starts) else len(source)
    text = source[line_start:line_end].decode("utf-8", errors="replace")
    return line_start + len(text[:col].encode("utf-8"))


def _receiver_text(source: bytes, line_starts: list[int], node: ast.expr) -> str:
    s = _col_to_byte(source, line_starts, node.lineno, node.col_offset)
    e = _col_to_byte(
        source, line_starts,
        node.end_lineno or node.lineno, node.end_col_offset or node.col_offset,
    )
    return source[s:e].decode("utf-8", errors="replace")


# ──────────────────────────────────────────────────────────────────────
# per-file resolver

class _PerFileResolver:
    """Resolves receiver expressions to class or module qpaths within one file."""

    def __init__(self, tree: ast.Module, module_prefix: str, index: IndexQuery):
        self.tree = tree
        self.module_prefix = module_prefix
        self.index = index

        # Parent links — AST nodes don't carry them by default.
        self.parent: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node

        # Local name → qualified class path. Built from imports and
        # same-file class declarations. Best-effort: complex import
        # forms (`import a.b.c`) are only partially handled.
        self.name_to_qpath: dict[str, str] = {}
        # Local name → module qpath. Used when resolving Attribute
        # receivers that point at modules (e.g. `m.foo` where m is an
        # imported module).
        self.module_name_to_qpath: dict[str, str] = {}
        # Scope-level local bindings, keyed by id(scope_node). Each
        # binding maps name → import_source_module (when the binding
        # came from `from X import leaf`) or None for other binding
        # forms (assign, param, def, etc.). The import_source lets us
        # tell apart "local rebinding of a different symbol" (shadow)
        # from "local import of the same target" (still our target).
        self.scope_locals: dict[int, dict[str, str | None]] = {}
        self.module_locals: dict[str, str | None] = {}
        self._build_name_table()
        self._build_scopes()

    def _build_name_table(self) -> None:
        for stmt in self.tree.body:
            if isinstance(stmt, ast.ImportFrom):
                src = stmt.module or ""
                for alias in stmt.names:
                    local = alias.asname or alias.name
                    qpath = f"{src}.{alias.name}" if src else alias.name
                    self.name_to_qpath[local] = qpath
                    # `from m import sub` could be either a name or a
                    # submodule. Treat it as both: if `sub.x` is used we
                    # need the module candidate; class qpath is also
                    # plausible. The module candidate falls out as the
                    # same qpath string.
                    self.module_name_to_qpath[local] = qpath
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.asname:
                        self.name_to_qpath[alias.asname] = alias.name
                        self.module_name_to_qpath[alias.asname] = alias.name
                    else:
                        # `import a.b.c` binds local name `a` to module `a`.
                        # Chains like `a.b.c.Foo` are handled by
                        # _resolve_attr_chain walking from `a` down.
                        head = alias.name.split(".")[0]
                        self.module_name_to_qpath[head] = head
        for stmt in ast.walk(self.tree):
            if isinstance(stmt, ast.ClassDef):
                chain = self._class_chain(stmt)
                if chain:
                    prefix = self.module_prefix
                    qpath = ".".join([prefix, *chain]) if prefix else ".".join(chain)
                    # Map only the leaf class name to its qpath. Nested
                    # classes shadow outer ones — last writer wins.
                    self.name_to_qpath[stmt.name] = qpath

    def _class_chain(self, node: ast.ClassDef) -> list[str]:
        chain: list[str] = []
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, ast.ClassDef):
                chain.append(cur.name)
            cur = self.parent.get(id(cur))
        chain.reverse()
        return chain

    def _enclosing_class(self, node: ast.AST) -> ast.ClassDef | None:
        cur: ast.AST | None = node
        while True:
            cur = self.parent.get(id(cur)) if cur is not None else None
            if cur is None:
                return None
            if isinstance(cur, ast.ClassDef):
                return cur

    def _enclosing_function(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        cur: ast.AST | None = node
        while True:
            cur = self.parent.get(id(cur)) if cur is not None else None
            if cur is None:
                return None
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur

    # ---------- annotation resolution

    def _resolve_attr_chain(self, node: ast.Attribute) -> str | None:
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        parts.append(cur.id)
        parts.reverse()
        head_qpath = self.name_to_qpath.get(parts[0])
        if head_qpath:
            return ".".join([head_qpath, *parts[1:]])
        return ".".join(parts)

    def _resolve_annotation(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.name_to_qpath.get(node.id) or node.id
        if isinstance(node, ast.Attribute):
            return self._resolve_attr_chain(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
            return self._resolve_annotation(inner)
        if isinstance(node, ast.Subscript):
            # `Optional[Foo]`, `list[Foo]` — these are container annotations,
            # not the type of `b` itself. Punt.
            return None
        return None

    # ---------- receiver resolution

    def resolve_receiver(self, recv: ast.expr) -> tuple[str | None, str | None]:
        """Returns (class_qpath, why). Exactly one is non-None."""
        if isinstance(recv, ast.Name):
            return self._resolve_name_receiver(recv)
        if isinstance(recv, ast.Attribute):
            qp = self._resolve_attr_chain(recv)
            if qp and self._looks_like_class_qpath(qp):
                return qp, None
            return None, f"attribute chain `{ast.unparse(recv)}` not resolved to a class"
        if isinstance(recv, ast.Call):
            if isinstance(recv.func, ast.Name) and recv.func.id == "super":
                return None, "super() receiver resolution not yet implemented"
            return None, "receiver bound to call expression — needs type checker"
        if isinstance(recv, ast.Subscript):
            return None, "receiver is a subscript expression — needs type checker"
        return None, f"unsupported receiver kind: {type(recv).__name__}"

    def _resolve_name_receiver(self, recv: ast.Name) -> tuple[str | None, str | None]:
        if recv.id in ("self", "cls"):
            cls = self._enclosing_class(recv)
            if cls is None:
                return None, f"`{recv.id}` outside any class"
            chain = self._class_chain(cls)
            prefix = self.module_prefix
            qp = ".".join([prefix, *chain]) if prefix else ".".join(chain)
            return qp, None

        binding = self._lookup_binding(recv)
        if binding is not None:
            return binding

        qp = self.name_to_qpath.get(recv.id)
        if qp:
            return qp, None
        return None, f"name `{recv.id}` not bound to a known class in scope"

    def _lookup_binding(
        self, name_node: ast.Name,
    ) -> tuple[str | None, str | None] | None:
        """Look for a binding of name_node.id in the enclosing function (or
        module). Returns:
          - None if no binding pattern matched (caller may fall through)
          - (qpath, None) if successfully resolved
          - (None, why) if a binding was found but unresolvable
        """
        name = name_node.id
        scope = self._enclosing_function(name_node)

        if scope is not None:
            for arg in (
                list(scope.args.posonlyargs)
                + list(scope.args.args)
                + list(scope.args.kwonlyargs)
            ):
                if arg.arg == name:
                    if arg.annotation is None:
                        return None, f"parameter `{name}` has no type annotation"
                    qp = self._resolve_annotation(arg.annotation)
                    if qp:
                        return qp, None
                    return None, f"parameter `{name}` annotation not resolvable"

        body_root: ast.AST = scope if scope is not None else self.tree
        for node in ast.walk(body_root):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return self._resolve_assign_value(node.value, name)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    qp = self._resolve_annotation(node.annotation)
                    if qp:
                        return qp, None
                    return None, f"`{name}` annotation not resolvable"
            elif isinstance(node, ast.For):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    return None, f"`{name}` is a for-loop variable — needs type checker"
        return None

    def _resolve_assign_value(
        self, value: ast.expr, target_name: str,
    ) -> tuple[str | None, str | None]:
        if isinstance(value, ast.Call):
            func = value.func
            if isinstance(func, ast.Name):
                qp = self.name_to_qpath.get(func.id)
                if qp:
                    return qp, None
                return None, f"`{target_name}` bound to `{func.id}()` — unknown class"
            if isinstance(func, ast.Attribute):
                qp = self._resolve_attr_chain(func)
                if qp:
                    return qp, None
                return None, f"`{target_name}` bound to attribute call — unresolved"
            return None, f"`{target_name}` bound to complex call expression"
        if isinstance(value, ast.Name):
            return None, f"`{target_name}` aliased from `{value.id}` — needs type checker"
        return None, f"`{target_name}` bound to non-constructor expression"

    # ---------- scope analysis (shadowing detection)

    def _build_scopes(self) -> None:
        """Populate scope_locals[id(fn)] for every Function/Lambda and
        module_locals for module-level bindings."""
        self.module_locals = self._locals_in(self.tree.body, include_def_names=True)
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.scope_locals[id(node)] = self._function_locals(node)
            elif isinstance(node, ast.Lambda):
                self.scope_locals[id(node)] = self._lambda_locals(node)

    def _function_locals(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        a = fn.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            out[arg.arg] = None
        if a.vararg:
            out[a.vararg.arg] = None
        if a.kwarg:
            out[a.kwarg.arg] = None
        for name, source in self._locals_in(fn.body, include_def_names=True).items():
            out[name] = source
        return out

    def _lambda_locals(self, lam: ast.Lambda) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        a = lam.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            out[arg.arg] = None
        return out

    def _locals_in(self, stmts: list[ast.stmt], *, include_def_names: bool) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for stmt in stmts:
            self._collect_locals(stmt, out, include_def_names=include_def_names)
        return out

    def _collect_locals(self, stmt: ast.stmt, out: dict[str, str | None], *, include_def_names: bool) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if include_def_names:
                out[stmt.name] = None
            return
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                self._add_target_names(t, out)
            return
        if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            self._add_target_names(stmt.target, out)
            return
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._add_target_names(stmt.target, out)
            for inner in stmt.body + stmt.orelse:
                self._collect_locals(inner, out, include_def_names=include_def_names)
            return
        if isinstance(stmt, (ast.While, ast.If)):
            for inner in stmt.body + stmt.orelse:
                self._collect_locals(inner, out, include_def_names=include_def_names)
            return
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    self._add_target_names(item.optional_vars, out)
            for inner in stmt.body:
                self._collect_locals(inner, out, include_def_names=include_def_names)
            return
        if isinstance(stmt, ast.Try):
            for inner in stmt.body:
                self._collect_locals(inner, out, include_def_names=include_def_names)
            for handler in stmt.handlers:
                if handler.name:
                    out[handler.name] = None
                for inner in handler.body:
                    self._collect_locals(inner, out, include_def_names=include_def_names)
            for inner in stmt.orelse + stmt.finalbody:
                self._collect_locals(inner, out, include_def_names=include_def_names)
            return
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                local = alias.asname or alias.name.split(".")[0]
                out[local] = alias.name
            return
        if isinstance(stmt, ast.ImportFrom):
            from_module = stmt.module or ""
            for alias in stmt.names:
                local = alias.asname or alias.name
                # Record the from-module so shadowing analysis can tell
                # local imports of the rename target apart from local
                # imports of unrelated symbols with the same leaf.
                out[local] = from_module
            return

    def _add_target_names(self, target: ast.expr, out: dict[str, str | None]) -> None:
        if isinstance(target, ast.Name):
            out[target.id] = None
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._add_target_names(elt, out)
        elif isinstance(target, ast.Starred):
            self._add_target_names(target.value, out)

    def classify_name(
        self, name_node: ast.Name, target_module_qpath: str,
    ) -> tuple[str, str | None]:
        """Walk enclosing scopes; return (verdict, scope_label).

        verdict ∈ {
          'shadow_other'  — local binding to an unrelated symbol; skip
          'local_target'  — local `from <target_module> import leaf`;
                            rewrite (it's still our target)
          'recursive'     — reference to the enclosing function whose
                            name IS leaf; rewrite (self-recursion)
          'unscoped'      — no local binding shadows; visibility
                            gate (module-level import vs declaring
                            file) decides whether to rewrite
        }
        """
        leaf = name_node.id
        cur: ast.AST | None = name_node
        while cur is not None:
            parent = self.parent.get(id(cur))
            if parent is None:
                return "unscoped", None
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_map = self.scope_locals.get(id(parent), {})
                if leaf in local_map:
                    if parent.name == leaf:
                        return "recursive", None
                    if self._binding_is_our_target(local_map[leaf], target_module_qpath):
                        return "local_target", None
                    return "shadow_other", f"function {parent.name!r}"
                cur = parent
                continue
            if isinstance(parent, ast.Lambda):
                local_map = self.scope_locals.get(id(parent), {})
                if leaf in local_map:
                    if self._binding_is_our_target(local_map[leaf], target_module_qpath):
                        return "local_target", None
                    return "shadow_other", "lambda"
                cur = parent
                continue
            cur = parent
        return "unscoped", None

    def _binding_is_our_target(self, source: str | None, target_module_qpath: str) -> bool:
        """True iff a local binding came from `from X import leaf` where
        X is our target module (direct or parent-package re-export)."""
        if not source:
            return False
        if source == target_module_qpath:
            return True
        if target_module_qpath.startswith(source + "."):
            return True
        return False

    def file_imports_target(self, leaf: str, target_module_qpath: str) -> bool:
        """True if the file imports `leaf` from target_module_qpath
        directly or via a parent package re-export."""
        for stmt in self.tree.body:
            if not isinstance(stmt, ast.ImportFrom):
                continue
            from_module = stmt.module or ""
            if not from_module:
                continue
            is_direct = from_module == target_module_qpath
            is_reexport = target_module_qpath.startswith(from_module + ".")
            if not (is_direct or is_reexport):
                continue
            for alias in stmt.names:
                if alias.name == leaf and alias.asname is None:
                    return True
        return False

    def _looks_like_class_qpath(self, qpath: str) -> bool:
        decl = self.index.find_declaration(qpath)
        if decl is None:
            return False
        return decl[3] == "class"

    # ---------- module receiver resolution

    def resolve_module_receiver(self, recv: ast.expr) -> str | None:
        """Resolve a receiver expression to a module qpath. Returns None
        if the receiver doesn't look like a module reference."""
        if isinstance(recv, ast.Name):
            return self.module_name_to_qpath.get(recv.id)
        if isinstance(recv, ast.Attribute):
            parts: list[str] = []
            cur: ast.expr = recv
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                return None
            head = self.module_name_to_qpath.get(cur.id)
            if head is None:
                return None
            parts.reverse()
            return ".".join([head, *parts])
        return None


# ──────────────────────────────────────────────────────────────────────
# declaration-site rewrite

def _decl_rewrite(
    source: bytes,
    line_starts: list[int],
    decl_range: tuple[int, int],
    leaf: str,
    new_name: str,
) -> ByteRewrite | None:
    leaf_b = leaf.encode("utf-8")
    tree = ast.parse(source)
    s_byte, e_byte = decl_range
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name != leaf:
            continue
        keyword_len = 6 if isinstance(node, ast.ClassDef) else (10 if isinstance(node, ast.AsyncFunctionDef) else 4)
        decl_byte = _col_to_byte(source, line_starts, node.lineno, node.col_offset)
        ident_start = decl_byte + keyword_len
        if not (s_byte <= ident_start < e_byte):
            continue
        if source[ident_start:ident_start + len(leaf_b)] != leaf_b:
            continue
        return ByteRewrite(
            byte_start=ident_start,
            byte_end=ident_start + len(leaf_b),
            new_text=new_name,
            line=node.lineno,
            col=node.col_offset + keyword_len,
            receiver_source="",
        )
    return None


# ──────────────────────────────────────────────────────────────────────
# public entry

def rename_member(
    *,
    source: bytes,
    leaf: str,
    new_name: str,
    target_qpath: SymbolPath,
    target_owner_qpath: SymbolPath,
    index: IndexQuery,
    is_declaring_file: bool,
    decl_byte_range: tuple[int, int] | None,
    module_prefix: str,
) -> RenameAnalysis:
    line_starts = line_byte_offsets(source)
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return RenameAnalysis(
            unresolved=(UnresolvedSite(
                byte_start=0, byte_end=0, line=e.lineno or 1, col=0,
                receiver_source="", why=f"file failed to parse: {e.msg}",
            ),),
        )

    rewrites: list[ByteRewrite] = []
    unresolved: list[UnresolvedSite] = []
    skipped: list[SkippedMismatchSite] = []

    if is_declaring_file and decl_byte_range is not None:
        decl = _decl_rewrite(source, line_starts, decl_byte_range, leaf, new_name)
        if decl is not None:
            rewrites.append(decl)

    fast_path = len(index.owners_of_leaf(leaf)) <= 1
    resolver = None if fast_path else _PerFileResolver(tree, module_prefix, index)

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == leaf):
            continue
        line = node.end_lineno or node.lineno
        attr_col = (node.end_col_offset or 0) - len(leaf)
        start = _col_to_byte(source, line_starts, line, attr_col)
        end = start + len(leaf.encode("utf-8"))
        recv_text = _receiver_text(source, line_starts, node.value)

        if fast_path:
            rewrites.append(ByteRewrite(
                byte_start=start, byte_end=end, new_text=new_name,
                line=line, col=attr_col, receiver_source=recv_text,
            ))
            continue

        qp, why = resolver.resolve_receiver(node.value)
        if qp is None:
            unresolved.append(UnresolvedSite(
                byte_start=start, byte_end=end,
                line=line, col=attr_col, receiver_source=recv_text,
                why=why or "unknown",
            ))
        elif qp == target_owner_qpath:
            rewrites.append(ByteRewrite(
                byte_start=start, byte_end=end, new_text=new_name,
                line=line, col=attr_col, receiver_source=recv_text,
            ))
        else:
            skipped.append(SkippedMismatchSite(
                byte_start=start, byte_end=end,
                line=line, col=attr_col, receiver_source=recv_text,
                resolved_to_qpath=qp,
            ))

    return RenameAnalysis(
        rewrites=tuple(rewrites),
        skipped_mismatch=tuple(skipped),
        unresolved=tuple(unresolved),
    )


# ──────────────────────────────────────────────────────────────────────
# module-binding rename (function / async_function / class / constant)

def rename_module_binding(
    *,
    source: bytes,
    leaf: str,
    new_name: str,
    target_qpath: SymbolPath,
    target_module_qpath: SymbolPath,
    index: IndexQuery,
    is_declaring_file: bool,
    decl_byte_range: tuple[int, int] | None,
    module_prefix: str,
) -> RenameAnalysis:
    """Per-file rename for module-level bindings.

    Walks three node shapes:
      - `Name(id==leaf)` → bare reference. Rewritten in declaring file
        (where the binding lives) or in files that imported `leaf`.
      - `Attribute(attr==leaf)` → module-qualified access. Receiver
        must resolve to `target_module_qpath` to rewrite; mismatched
        receivers go to skipped_mismatch.
      - `alias(name==leaf)` inside `from M import leaf` → import
        rewrite. The from-module must equal `target_module_qpath`.

    Scope analysis (shadowing detection) is not implemented in v1 — a
    local `leaf = 1` in some function will still be rewritten. This
    matches the prior regex behavior; surfacing shadowing is a
    follow-up.
    """
    line_starts = line_byte_offsets(source)
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return RenameAnalysis(
            unresolved=(UnresolvedSite(
                byte_start=0, byte_end=0, line=e.lineno or 1, col=0,
                receiver_source="", why=f"file failed to parse: {e.msg}",
            ),),
        )

    rewrites: list[ByteRewrite] = []
    unresolved: list[UnresolvedSite] = []
    skipped: list[SkippedMismatchSite] = []

    if is_declaring_file and decl_byte_range is not None:
        decl = _decl_rewrite(source, line_starts, decl_byte_range, leaf, new_name)
        if decl is not None:
            rewrites.append(decl)

    resolver = _PerFileResolver(tree, module_prefix, index)
    leaf_b = leaf.encode("utf-8")
    file_imports = resolver.file_imports_target(leaf, target_module_qpath)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == leaf:
            start = _col_to_byte(source, line_starts, node.lineno, node.col_offset)
            end = start + len(leaf_b)

            verdict, scope_label = resolver.classify_name(node, target_module_qpath)

            if verdict == "shadow_other":
                skipped.append(SkippedMismatchSite(
                    byte_start=start, byte_end=end,
                    line=node.lineno, col=node.col_offset, receiver_source="",
                    resolved_to_qpath=f"<local in {scope_label}>",
                ))
                continue

            if verdict in ("local_target", "recursive"):
                rewrites.append(ByteRewrite(
                    byte_start=start, byte_end=end, new_text=new_name,
                    line=node.lineno, col=node.col_offset, receiver_source="",
                ))
                continue

            # verdict == "unscoped": no local binding shadows this name.
            # Visibility gate: a bare reference resolves to our target
            # only if the file is the declaring module OR imports leaf
            # from our module at module level. Otherwise the file has
            # its own module-level `leaf` (e.g. `from other import leaf`)
            # — surface as skipped_mismatch.
            if not (is_declaring_file or file_imports):
                if leaf in resolver.module_locals:
                    skipped.append(SkippedMismatchSite(
                        byte_start=start, byte_end=end,
                        line=node.lineno, col=node.col_offset, receiver_source="",
                        resolved_to_qpath=f"<module-local in {module_prefix or '<file>'}>",
                    ))
                continue

            rewrites.append(ByteRewrite(
                byte_start=start, byte_end=end, new_text=new_name,
                line=node.lineno, col=node.col_offset, receiver_source="",
            ))

        elif isinstance(node, ast.Attribute) and node.attr == leaf:
            recv_module = resolver.resolve_module_receiver(node.value)
            line = node.end_lineno or node.lineno
            attr_col = (node.end_col_offset or 0) - len(leaf)
            start = _col_to_byte(source, line_starts, line, attr_col)
            end = start + len(leaf_b)
            recv_text = _receiver_text(source, line_starts, node.value)
            if recv_module == target_module_qpath:
                rewrites.append(ByteRewrite(
                    byte_start=start, byte_end=end, new_text=new_name,
                    line=line, col=attr_col, receiver_source=recv_text,
                ))
            elif recv_module is not None:
                skipped.append(SkippedMismatchSite(
                    byte_start=start, byte_end=end,
                    line=line, col=attr_col, receiver_source=recv_text,
                    resolved_to_qpath=f"{recv_module}.{leaf}",
                ))
            # recv_module is None → receiver isn't a module reference
            # (probably an instance attribute access). Silently skip.

        elif isinstance(node, ast.alias) and node.name == leaf:
            parent = resolver.parent.get(id(node))
            if isinstance(parent, ast.ImportFrom):
                from_module = parent.module or ""
                # Accept the alias when:
                #  (a) the from-module is exactly the target module, OR
                #  (b) the target module is a submodule of from_module
                #      (parent package re-export via __init__.py).
                # The re-export case is heuristic — it may over-match
                # in rare cases where a parent package legitimately
                # owns its own same-named symbol. Surface those as
                # `unresolved` rather than `skipped_mismatch` so the
                # agent reviews them.
                is_direct = from_module == target_module_qpath
                is_reexport = bool(from_module) and target_module_qpath.startswith(from_module + ".")
                start = _col_to_byte(source, line_starts, node.lineno, node.col_offset)
                end = start + len(leaf_b)
                if is_direct or is_reexport:
                    rewrites.append(ByteRewrite(
                        byte_start=start, byte_end=end, new_text=new_name,
                        line=node.lineno, col=node.col_offset, receiver_source="",
                    ))
                elif from_module:
                    skipped.append(SkippedMismatchSite(
                        byte_start=start, byte_end=end,
                        line=node.lineno, col=node.col_offset, receiver_source="",
                        resolved_to_qpath=f"{from_module}.{leaf}",
                    ))

    return RenameAnalysis(
        rewrites=tuple(rewrites),
        skipped_mismatch=tuple(skipped),
        unresolved=tuple(unresolved),
    )
