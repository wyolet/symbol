"""SymbolIndex — compact columnar AST lookup table.

Rows are plain tuples. No wrapper classes anywhere in the storage layer.
Strings live in one shared intern pool; rows carry integer ids into it.

Refs are grouped per symbol — each symbol has its own list of refs, and
each ref is a 2-tuple (packed_name_and_kind, line). The source_row is
implicit (it's the outer list index). Refs also skip locals, params, and
Python builtins, so the list is the agent-relevant name dependencies only.

Persistence: msgpack + zstd. Safe (no code execution), portable, fast.
"""

import subprocess
from pathlib import Path

import msgpack
import zstandard as zstd

from wyolet.symbol.shared.ast_cache import ASTCache
from wyolet.symbol.shared.symbol import (
    I_FILE, I_LINE, I_NAME, I_SOURCE,
    REF_ATTR, REF_LABELS, REF_NAME,
    S_EBYTE, S_ELINE, S_FILE, S_KIND, S_LANG,
    S_PARENT, S_PATH, S_SBYTE, S_SLINE,
)

_INDEX_PATH = Path(".symbol") / "symbol_index.msgpack.zst"
_INDEX_VERSION = 6
_ZSTD_LEVEL = 3
# If more files than this are stale, full rebuild is cheaper than patching.
_REBUILD_THRESHOLD = 200
# Auto-compact once dead rows pass both thresholds (ratio + absolute floor).
_COMPACT_RATIO = 0.30
_COMPACT_MIN = 100

# ---------------------------------------------------------- ref bit-packing

def _pack_ref(name_id: int, kind: int) -> int:
    return (name_id << 1) | (kind & 1)


def _unpack_ref(packed: int) -> tuple[int, int]:
    return packed >> 1, packed & 1


# ---------------------------------------------------------- index

class SymbolIndex:
    """Compact columnar symbol lookup table."""

    def __init__(self, cache: ASTCache | None = None, project_root: Path | None = None):
        if cache is not None:
            self.cache = cache
            self.project_root = cache.project_root
        else:
            assert project_root is not None
            self.cache = None
            self.project_root = project_root

        # String intern pool. 0 = empty string.
        self.strings: list[str] = [""]
        self._str_id: dict[str, int] = {"": 0}

        # Dimension tables.
        self.files: list[str] = []
        self._file_ids: dict[str, int] = {}
        # Per-file language: parallel to self.files. Each entry indexes into
        # self.langs. Reuses the existing intern pool — effectively one byte
        # per file for a codebase with <256 distinct languages.
        self.file_langs: list[int] = []
        self.kinds: list[str] = []
        self._kind_ids: dict[str, int] = {}
        self.langs: list[str] = []
        self._lang_ids: dict[str, int] = {}

        # Row stores — plain tuples.
        #   symbol: (path_id, file_id, s_line, e_line, s_byte, e_byte, kind_id, lang_id, parent)
        #   import: (file_id, name_id, source_id, line)
        self.symbols: list[tuple] = []
        self.imports: list[tuple] = []

        # Refs grouped per symbol row. Same length as self.symbols.
        #   refs_of[row] = list[(packed_name_kind, line)]
        self.refs_of: list[list[tuple[int, int]]] = []

        # Resolvers.
        self.by_path: dict[str, list[int]] = {}
        self.by_file: dict[int, list[int]] = {}
        self.imps_by_file: dict[int, list[int]] = {}
        #   by_name_id: name_id -> list[(source_row, line, kind)]
        #   rebuilt on load from refs_of.
        self.by_name_id: dict[int, list[tuple[int, int, int]]] = {}

        # Staleness-tracking state (populated on save/load).
        self._saved_mtimes: dict[str, float] = {}
        self._saved_dir_mtimes: dict[str, float] = {}
        self._git_head: str = ""
        self._scope_include: tuple[str, ...] = ()
        self._scope_exclude: tuple[str, ...] = ()

        # Tombstone counter — compaction trigger.
        self._dead_rows: int = 0

        self._built = False

    # ---------------------------------------------------------- interning

    def _str(self, s: str) -> int:
        sid = self._str_id.get(s)
        if sid is None:
            sid = len(self.strings)
            self.strings.append(s)
            self._str_id[s] = sid
        return sid

    def _kind(self, k: str) -> int:
        kid = self._kind_ids.get(k)
        if kid is None:
            kid = len(self.kinds)
            self.kinds.append(k)
            self._kind_ids[k] = kid
        return kid

    def _lang(self, l: str) -> int:
        lid = self._lang_ids.get(l)
        if lid is None:
            lid = len(self.langs)
            self.langs.append(l)
            self._lang_ids[l] = lid
        return lid

    def _file_id(self, rel: str, language: str) -> int:
        fid = self._file_ids.get(rel)
        if fid is None:
            fid = len(self.files)
            self.files.append(rel)
            self._file_ids[rel] = fid
            self.file_langs.append(self._lang(language))
        return fid

    # ---------------------------------------------------------- build

    def build(self) -> None:
        if self._built:
            return
        assert self.cache is not None, "build() requires an ASTCache"
        for path in self.cache.files:
            self._walk_file(path)
        self._build_by_name_id()
        self._built = True

    def _walk_file(self, path: Path) -> None:
        """Ingest one file into the index via the language adapter.

        Reads bytes, runs ``adapter.scan_file`` with a layout-aware module
        prefix, then flattens the resulting ``FileScan`` into the columnar
        tables. The adapter owns all AST work; this method is pure I/O +
        bookkeeping.
        """
        from wyolet.symbol.adapters import default_registry
        from wyolet.symbol.adapters.registry import UnsupportedLanguage

        rel = str(path.relative_to(self.project_root))
        try:
            source = path.read_bytes()
        except OSError:
            return

        # Prefer the pre-classified language from the project-wide linguist
        # pass (full builds always have a cache). Refresh paths load the
        # index from disk with no cache and fall back to per-file detection
        # inside ``registry.for_file``.
        language = self.cache.language_of(path) if self.cache is not None else None
        try:
            if language is not None:
                adapter = default_registry().for_language(language)
            else:
                adapter = default_registry().for_file(path)
        except UnsupportedLanguage:
            return
        scan = adapter.scan_file(path, source, module_prefix=adapter.module_prefix(rel))
        if not scan.ok:
            return

        file_id = self._file_id(rel, language=scan.language)
        lang_id = self._lang(scan.language)

        for imp in scan.imports:
            imp_idx = len(self.imports)
            self.imports.append(
                (file_id, self._str(imp.local), self._str(imp.source), imp.line)
            )
            self.imps_by_file.setdefault(file_id, []).append(imp_idx)

        for top in scan.symbols:
            self._ingest_symbol(top, file_id=file_id, parent_row=-1, lang_id=lang_id)

    def _ingest_symbol(self, sym, *, file_id: int, parent_row: int, lang_id: int) -> None:
        row_idx = len(self.symbols)
        start_byte, end_byte = sym.byte_range
        start_line, end_line = sym.line_range

        self.symbols.append(
            (
                self._str(sym.qualified_path),
                file_id,
                start_line,
                end_line,
                start_byte,
                end_byte,
                self._kind(sym.kind),
                lang_id,
                parent_row,
            )
        )

        packed_refs: list[tuple[int, int]] = []
        for r in sym.refs:
            kind_bit = REF_NAME if r.kind == "name" else REF_ATTR
            packed_refs.append((_pack_ref(self._str(r.name), kind_bit), r.line))
        self.refs_of.append(packed_refs)
        self.by_path.setdefault(sym.qualified_path, []).append(row_idx)
        self.by_file.setdefault(file_id, []).append(row_idx)

        for child in sym.children:
            self._ingest_symbol(
                child, file_id=file_id, parent_row=row_idx, lang_id=lang_id
            )

    def _build_by_name_id(self) -> None:
        by: dict[int, list[tuple[int, int, int]]] = {}
        for src_row, refs in enumerate(self.refs_of):
            for packed, line in refs:
                name_id, kind = _unpack_ref(packed)
                by.setdefault(name_id, []).append((src_row, line, kind))
        self.by_name_id = by

    # ---------------------------------------------------------- accessors

    def num_symbols(self) -> int:
        return len(self.symbols)

    def num_refs(self) -> int:
        return sum(len(r) for r in self.refs_of)

    def path_of(self, row: int) -> str:
        return self.strings[self.symbols[row][S_PATH]]

    def kind_of(self, row: int) -> str:
        return self.kinds[self.symbols[row][S_KIND]]

    def language_of(self, row: int) -> str:
        return self.langs[self.symbols[row][S_LANG]]

    def range_of(self, row: int) -> tuple[int, int]:
        s = self.symbols[row]
        return s[S_SLINE], s[S_ELINE]

    def file_of(self, row: int) -> str:
        return self.files[self.symbols[row][S_FILE]]

    def language_of_file(self, path: str) -> str | None:
        """Language for a repo-relative path, or None if not indexed."""
        fid = self._file_ids.get(path)
        if fid is None:
            return None
        return self.langs[self.file_langs[fid]]

    def parent_of(self, row: int) -> int:
        return self.symbols[row][S_PARENT]

    def imports_for(self, file_id: int) -> list[tuple[str, str, int]]:
        """(name, source, line) per import binding in this file."""
        out: list[tuple[str, str, int]] = []
        for i in self.imps_by_file.get(file_id, []):
            imp = self.imports[i]
            out.append(
                (self.strings[imp[I_NAME]], self.strings[imp[I_SOURCE]], imp[I_LINE])
            )
        return out

    def refs_for(self, row: int) -> list[tuple[str, str, int]]:
        """(name, kind_label, line) per ref, deduped per body."""
        out: list[tuple[str, str, int]] = []
        for packed, line in self.refs_of[row]:
            name_id, kind = _unpack_ref(packed)
            out.append((self.strings[name_id], REF_LABELS[kind], line))
        return out

    def callers_of(self, name: str) -> list[tuple[int, int, str]]:
        """(source_row, line, kind_label) for every ref whose name matches.

        O(1) on name lookup via by_name_id; O(hits) on the result.
        """
        nid = self._str_id.get(name)
        if nid is None:
            return []
        return [
            (src, line, REF_LABELS[kind])
            for src, line, kind in self.by_name_id.get(nid, [])
        ]

    def body(self, row: int) -> str:
        s = self.symbols[row]
        abs_path = self.project_root / self.files[s[S_FILE]]
        with open(abs_path, "rb") as f:
            f.seek(s[S_SBYTE])
            return f.read(s[S_EBYTE] - s[S_SBYTE]).decode("utf-8", errors="replace")

    def signature(self, row: int) -> str:
        """Declaration line(s) only. Delegates to the row's language adapter.

        The index reads the leading bytes of the symbol's body; the adapter
        decides where the declaration ends (``:`` for Python, ``{`` for Go).
        """
        from wyolet.symbol.adapters import default_registry
        from wyolet.symbol.adapters.registry import UnsupportedLanguage

        s = self.symbols[row]
        abs_path = self.project_root / self.files[s[S_FILE]]
        span = min(s[S_EBYTE] - s[S_SBYTE], 4096)
        with open(abs_path, "rb") as f:
            f.seek(s[S_SBYTE])
            data = f.read(span)
        text = data.decode("utf-8", errors="replace")
        language = self.language_of(row)
        try:
            adapter = default_registry().for_language(language)
        except UnsupportedLanguage:
            return text.splitlines()[0].strip() if text else ""
        return adapter.signature_from_text(text)

    # ---------------------------------------------------------- composite row ops
    #
    # Shaping helpers: tree building, payload assembly, raw file slicing.
    # Language-agnostic — operate on index rows, not AST. Language-specific
    # text work (preview, docstring stripping) lives on the adapter.

    def descendants_of(self, root: int) -> list[int]:
        """Every row reachable via parent chain from `root` (excludes root)."""
        out: list[int] = []
        stack = [root]
        seen = {root}
        while stack:
            parent = stack.pop()
            for i, sym in enumerate(self.symbols):
                if sym[S_PARENT] == parent and i not in seen:
                    seen.add(i)
                    out.append(i)
                    stack.append(i)
        return out

    def build_tree(self, row_ids: list[int], root_row: int | None = None) -> list[dict]:
        """Turn a flat list of row ids into nested dicts via parent pointers."""
        nodes: dict[int, dict] = {}
        roots: list[dict] = []
        for row in row_ids:
            s, e = self.range_of(row)
            node = {
                "path": self.path_of(row),
                "kind": self.kind_of(row),
                "signature": self.signature(row),
                "start_line": s,
                "end_line": e,
                "children": [],
            }
            nodes[row] = node
            parent_row = self.parent_of(row)
            parent = nodes.get(parent_row)
            if parent is None or row == root_row:
                roots.append(node)
            else:
                parent["children"].append(node)
        return roots

    def row_payload(self, row: int) -> dict:
        """Full payload for a symbol row: body + imports + refs, deduped."""
        s, e = self.range_of(row)

        raw_refs = self.refs_for(row)
        ref_index: dict[tuple[str, str], dict] = {}
        for n, k, ln in raw_refs:
            key = (n, k)
            existing = ref_index.get(key)
            if existing is None:
                ref_index[key] = {"name": n, "kind": k, "line": ln, "lines": [ln]}
            else:
                existing["lines"].append(ln)

        used_names = {n for n, k, _ in raw_refs if k == "name"}
        file_id = self.symbols[row][S_FILE]
        imports = [
            {"name": n, "source": src, "line": ln}
            for n, src, ln in self.imports_for(file_id)
            if n in used_names
        ]

        return {
            "path": self.path_of(row),
            "file": self.file_of(row),
            "start_line": s,
            "end_line": e,
            "kind": self.kind_of(row),
            "language": self.language_of(row),
            "signature": self.signature(row),
            "body": self.body(row),
            "imports": imports,
            "refs": list(ref_index.values()),
        }

    def raw_slice(self, file: str, start: int, end: int) -> dict:
        """Payload for a line range with no matching indexed symbol."""
        abs_path = self.project_root / file
        with open(abs_path, "rb") as f:
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        body = "\n".join(lines[start - 1 : end])
        return {
            "path": None,
            "file": file,
            "start_line": start,
            "end_line": end,
            "kind": "slice",
            "language": self.language_of_file(file),
            "signature": None,
            "body": body,
            "imports": [],
            "refs": [],
        }

    @property
    def stats(self) -> dict:
        return {
            "files": len(self.files),
            "symbols": len(self.symbols),
            "live_symbols": len(self.symbols) - self._dead_rows,
            "dead_rows": self._dead_rows,
            "imports": len(self.imports),
            "refs": self.num_refs(),
            "strings": len(self.strings),
        }

    # ---------------------------------------------------------- persistence

    def _snapshot_mtimes(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for rel in self.files:
            try:
                out[rel] = (self.project_root / rel).stat().st_mtime
            except OSError:
                out[rel] = 0.0
        return out

    def _snapshot_dir_mtimes(self) -> dict[str, float]:
        """mtimes of every directory that holds at least one indexed file.

        A directory's mtime changes when entries are added / removed / renamed
        inside it, so this catches file additions without needing git.
        """
        dirs: set[str] = set()
        for rel in self.files:
            parent = str(Path(rel).parent)
            if parent and parent != ".":
                dirs.add(parent)
        dirs.add(".")  # project root itself
        out: dict[str, float] = {}
        for d in dirs:
            try:
                out[d] = (self.project_root / d).stat().st_mtime
            except OSError:
                out[d] = 0.0
        return out

    def _current_head(self) -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_root,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return ""

    def _git_commit_changes(self, from_sha: str) -> set[str]:
        """Repo-relative paths changed between `from_sha` and current HEAD."""
        if not from_sha:
            return set()
        try:
            out = subprocess.check_output(
                ["git", "diff", "--name-only", f"{from_sha}..HEAD"],
                cwd=self.project_root,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return {line for line in out.splitlines() if line}
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return set()

    def _git_tracked_sources(self) -> set[str] | None:
        """Every path git knows about that linguist classifies as a language we
        have an adapter for. Tracked + untracked, respecting .gitignore.

        No extension filter: git enumerates, linguist classifies, the adapter
        registry decides eligibility. This is the only extension-free path
        for new-file discovery in incremental refresh.
        """
        try:
            tracked = subprocess.check_output(
                ["git", "ls-files", "-z"],
                cwd=self.project_root,
                stderr=subprocess.DEVNULL,
            ).decode()
            untracked = subprocess.check_output(
                ["git", "ls-files", "-z", "--others", "--exclude-standard"],
                cwd=self.project_root,
                stderr=subprocess.DEVNULL,
            ).decode()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return None
        rels = {p for p in tracked.split("\0") if p}
        rels |= {p for p in untracked.split("\0") if p}

        from wyolet.symbol.adapters import default_registry
        from wyolet.symbol.shared.linguist.blob import Blob
        from wyolet.symbol.shared.linguist.linguist import Linguist

        linguist = Linguist()
        registry = default_registry()
        out: set[str] = set()
        for rel in rels:
            abs_path = self.project_root / rel
            try:
                blob = Blob(str(abs_path))
            except (OSError, IsADirectoryError):
                continue
            lang = linguist.detect(blob)
            if lang is None or lang.name == "Unknown":
                continue
            if not registry.has_adapter(lang.key):
                continue
            out.add(rel)
        return out

    def _in_scope(self, rel: str) -> bool:
        """Apply saved include/exclude glob patterns to a repo-relative path."""
        from fnmatch import fnmatch
        if self._scope_exclude and any(fnmatch(rel, p) for p in self._scope_exclude):
            return False
        if self._scope_include and not any(fnmatch(rel, p) for p in self._scope_include):
            return False
        return True

    def stale_files(self) -> tuple[set[str], set[str]]:
        """Compute (stale, deleted) without scanning the project directory.

        Uses git for file enumeration (fast — one ``ls-files`` call) and the
        saved scope patterns to filter. Falls back to mtime-only detection
        when git isn't available: only looks at files we already knew about,
        which means new files won't be auto-discovered until ``symbol index``.
        """
        saved = set(self.files)
        stale: set[str] = set()

        current_head = self._current_head()
        head_advanced = bool(self._git_head and current_head and self._git_head != current_head)

        # 1) Git commit diff — covers branch switches / rebases quickly.
        if head_advanced:
            stale |= self._git_commit_changes(self._git_head)

        # 2) Mtime scan over files we already know about.
        for rel in saved:
            saved_mt = self._saved_mtimes.get(rel, 0.0)
            try:
                if (self.project_root / rel).stat().st_mtime != saved_mt:
                    stale.add(rel)
            except OSError:
                stale.add(rel)

        # 3) Enumerate current in-scope files to detect additions and deletions.
        current = self._git_tracked_sources()
        if current is None:
            # No git — we can't cheaply enumerate. Added/deleted detection
            # silently skipped on the incremental path; next ``symbol index``
            # fixes it. Mtime-detected changes still flow through.
            return stale & saved, set()

        current = {p for p in current if self._in_scope(p)}
        deleted = saved - current
        added = current - saved
        stale |= added

        # stale must be a subset of currently present files.
        stale &= current

        return stale, deleted

    def ensure_fresh(self, rel: str) -> bool:
        """Refresh `rel` in place if its on-disk mtime differs from the index's.

        Returns True if a refresh was performed. Cheap mtime check up front so
        the common case (no change) costs one stat call. Called by write
        operations before they read symbol byte ranges, to prevent splices
        against stale offsets when the file has been edited out of band
        (Patch above the symbol, plain Edit/Write, git checkout, etc.).
        """
        path = self.project_root / rel
        try:
            current = path.stat().st_mtime
        except OSError:
            return False
        saved = self._saved_mtimes.get(rel)
        if saved is not None and saved == current:
            return False
        self.refresh(stale={rel})
        return True

    # ---------------------------------------------------------- refresh

    def refresh(
        self,
        stale: set[str] | None = None,
        deleted: set[str] | None = None,
    ) -> None:
        """Patch the index in place. Parses stale files directly with ``ast``;
        no project scanner or build_context involved.
        """
        stale = stale or set()
        deleted = deleted or set()

        for rel in deleted | stale:
            self._tombstone_file(rel)

        for rel in stale:
            path = self.project_root / rel
            if not path.exists():
                continue
            self._walk_file(path)

        self._build_by_name_id()

        self._saved_mtimes = self._snapshot_mtimes()
        self._saved_dir_mtimes = self._snapshot_dir_mtimes()
        self._git_head = self._current_head()

    def _tombstone_file(self, rel: str) -> None:
        """Mark all rows for `rel` dead and unlink from resolvers.

        Row positions stay stable so parent_row references on surviving rows
        don't break. Dead rows get `path_id=0` and empty refs; queries skip
        them via the resolver dicts.
        """
        fid = self._file_ids.get(rel)
        if fid is None:
            return

        dead_rows = list(self.by_file.get(fid, []))
        for row in dead_rows:
            old = self.symbols[row]
            path = self.strings[old[0]] if old[0] else ""
            # Tombstone symbol row — keep file_id for debugging, zero out the rest.
            self.symbols[row] = (0, fid, 0, 0, 0, 0, 0, 0, -1)
            self.refs_of[row] = []
            if path and path in self.by_path:
                remaining = [r for r in self.by_path[path] if r != row]
                if remaining:
                    self.by_path[path] = remaining
                else:
                    del self.by_path[path]
        self._dead_rows += len(dead_rows)

        self.by_file.pop(fid, None)

        dead_imps = list(self.imps_by_file.get(fid, []))
        for i in dead_imps:
            self.imports[i] = (fid, 0, 0, 0)
        self.imps_by_file.pop(fid, None)

        self._saved_mtimes.pop(rel, None)

    def _payload(self) -> dict:
        return {
            "v": _INDEX_VERSION,
            "root": str(self.project_root),
            "strings": self.strings,
            "files": self.files,
            "file_langs": self.file_langs,
            "kinds": self.kinds,
            "langs": self.langs,
            "symbols": self.symbols,
            "imports": self.imports,
            "refs_of": self.refs_of,
            "by_path": self.by_path,
            "by_file": self.by_file,
            "imps_by_file": self.imps_by_file,
            "mtimes": self._snapshot_mtimes(),
            "dir_mtimes": self._snapshot_dir_mtimes(),
            "git_head": self._current_head(),
            "scope_include": list(self._scope_include),
            "scope_exclude": list(self._scope_exclude),
            "dead_rows": self._dead_rows,
        }

    def save(self, path: Path | None = None) -> Path:
        target = (self.project_root / (path or _INDEX_PATH)).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        packed = msgpack.packb(self._payload(), use_bin_type=True)
        compressed = zstd.ZstdCompressor(level=_ZSTD_LEVEL).compress(packed)
        target.write_bytes(compressed)
        return target

    @classmethod
    def load(cls, project_root: Path, path: Path | None = None) -> "SymbolIndex | None":
        target = (project_root / (path or _INDEX_PATH)).resolve()
        if not target.exists():
            return None
        try:
            raw = target.read_bytes()
            packed = zstd.ZstdDecompressor().decompress(raw)
            payload = msgpack.unpackb(packed, raw=False, strict_map_key=False)
        except (OSError, msgpack.exceptions.UnpackException, zstd.ZstdError, ValueError):
            return None
        if payload.get("v") != _INDEX_VERSION:
            return None
        if payload.get("root") != str(project_root):
            return None

        idx = cls(project_root=project_root)
        # Remember staleness state so callers can decide whether to refresh.
        idx._saved_mtimes = payload.get("mtimes", {})
        idx._saved_dir_mtimes = payload.get("dir_mtimes", {})
        idx._git_head = payload.get("git_head", "")
        idx._scope_include = tuple(payload.get("scope_include", ()))
        idx._scope_exclude = tuple(payload.get("scope_exclude", ()))
        idx._dead_rows = int(payload.get("dead_rows", 0))
        idx.strings = payload["strings"]
        # Rebuild reverse map so name-based queries (callers_of) work after load.
        idx._str_id = {s: i for i, s in enumerate(idx.strings)}
        idx.files = payload["files"]
        idx._file_ids = {f: i for i, f in enumerate(idx.files)}
        idx.file_langs = list(payload["file_langs"])
        idx.kinds = payload["kinds"]
        idx._kind_ids = {k: i for i, k in enumerate(idx.kinds)}
        idx.langs = payload["langs"]
        idx._lang_ids = {l: i for i, l in enumerate(idx.langs)}
        # msgpack returns lists-of-lists for tuple rows; queries treat them as tuples.
        idx.symbols = [tuple(s) for s in payload["symbols"]]
        idx.imports = [tuple(i) for i in payload["imports"]]
        idx.refs_of = [[tuple(r) for r in rs] for rs in payload["refs_of"]]
        idx.by_path = payload["by_path"]
        idx.by_file = {int(k): v for k, v in payload["by_file"].items()}
        idx.imps_by_file = {int(k): v for k, v in payload["imps_by_file"].items()}
        idx._build_by_name_id()
        idx._built = True
        return idx


def get_or_build_index(project_root: Path) -> tuple["SymbolIndex", str]:
    """Load, refresh incrementally, or rebuild.

    Returns (index, source) where source is one of:
      "disk"    — nothing changed, served straight from disk
      "refresh" — some files changed, incremental patch applied
      "rebuild" — no cached index (or too much changed), full rebuild
    """
    existing = SymbolIndex.load(project_root)
    if existing is None:
        return _full_build(project_root)

    if _quick_is_clean(existing):
        return existing, "disk"

    # Enumerate changes without touching the project scanner.
    stale, deleted = existing.stale_files()

    if not stale and not deleted:
        return existing, "disk"

    if len(stale) + len(deleted) > _REBUILD_THRESHOLD:
        return _full_build(project_root, tag="rebuild")

    existing.refresh(stale=stale, deleted=deleted)

    # Auto-compact if tombstones have accumulated past both the ratio
    # and absolute floor thresholds.
    total = len(existing.symbols)
    dead = existing._dead_rows
    if dead >= _COMPACT_MIN and total > 0 and dead / total >= _COMPACT_RATIO:
        return _full_build(project_root, tag="compact")

    existing.save()
    return existing, "refresh"


def _quick_is_clean(idx: "SymbolIndex") -> bool:
    """Fast probe — zero git subprocesses, all in-process stat().

    Measured on macOS SSD: ``os.stat`` costs ~5μs (kernel VFS cache);
    any ``git`` subprocess is ~13 ms just to spawn. So we skip git entirely.

      1) mtime on known files — edits in place (O(indexed files))
      2) mtime on their directories — additions / removals / renames in those
         dirs (O(indexed dirs), far fewer than files)

    Holes we accept for this tier of speed:
      * Branch switch where working-tree content matches byte-for-byte with
        identical mtimes — virtually never happens since git rewrites files.
      * New file in a directory the index has never seen — rare for agents
        editing established projects; a manual ``symbol index`` resolves it.

    Anything that fails this probe falls through to the exact diff path,
    which does consult git for precise, batched change detection.
    """
    for rel, saved_mt in idx._saved_mtimes.items():
        try:
            if (idx.project_root / rel).stat().st_mtime != saved_mt:
                return False
        except OSError:
            return False

    for d, saved_mt in idx._saved_dir_mtimes.items():
        try:
            if (idx.project_root / d).stat().st_mtime != saved_mt:
                return False
        except OSError:
            return False

    return True


def _full_build(project_root: Path, tag: str = "build") -> tuple["SymbolIndex", str]:
    from wyolet.symbol.shared.context import build_context
    ctx = build_context(project_root)
    idx = SymbolIndex(ctx.cache)
    idx.build()
    idx.save()
    return idx, tag


