# symbol-linguist (Rust)

Faithful Rust port of symbol's GitHub-Linguist subsystem
(`src/wyolet/symbol/shared/linguist/`). The Python modules are the spec;
each Rust module mirrors its counterpart one-to-one (`blob.rs` ← `blob.py`,
`strategy/modeline.rs` ← `strategy/modeline.py`, …).

```
cargo build --release          # from rust/
./target/release/symbol-linguist <path> [--json] [--files] [--classify]
    [--exclude GLOB]... [--no-vendor-skip] [--no-heuristics] [--no-cache]
    [--config-dir DIR] [-v]
```

`--json` matches `symbol loc --format json` byte-for-byte in shape
(summary / languages / biggest_files). Parity is enforced by
`scripts/linguist_parity.py`:

```
uv run scripts/linguist_parity.py <target>                # gate: exact match
uv run scripts/linguist_parity.py <target> --heuristics   # documented delta
```

## Detection chain

ModeLine → Filename → Shebang → Extension → XML → Manpage → **Heuristics**,
then popular-preference and `_best_candidate` (the `_PREFERRED` table +
specificity sort) — identical to `linguist.py` except Heuristics, which
Python vendored (`heuristics.yml`) but never wired into
`Linguist.strategies`. Upstream github-linguist runs Heuristics after
Manpage; it is wired there. `--no-heuristics` reproduces the Python chain
exactly — that is the parity gate configuration.

## Expected divergences (all opt-in via heuristics, or unreachable in Python)

1. **Heuristics enabled by default** — content disambiguation reclassifies
   ambiguous extensions (`.h` C/C++/Obj-C, `.txt` Text vs Vim Help File,
   `.sql` SQL/TSQL/PLSQL, `.rs`, `.d`, `.pl`, …). Verified deltas on real
   repos move whole files between sibling languages; counts stay exact.
2. Three latent Python heuristics bugs fixed in the port (unreachable in
   Python since the strategy is unwired): YAML array patterns union as
   `(?:a)|(?:b)` instead of crashing; matching is unanchored (Ruby `=~`
   semantics, not `re.match`); a rule's `language:` list matches any listed
   language among the candidates.
3. Patterns neither `regex` nor `fancy-regex` can compile drop their rule,
   counted and shown with `-v` (same policy as Python's
   `load_vendor_regex`). Currently 1 heuristics rule drops.
4. Unreadable files (e.g. broken symlinks) are skipped; Python's Blob would
   raise.

## Config

The six linguist YAMLs resolve from `--config-dir`, then
`$SYMBOL_LINGUIST_CONFIG_DIR`, then copies embedded at build time from the
Python package's config dir. Point `--config-dir` at
`src/wyolet/symbol/shared/linguist/config` to pick up `symbol
update-linguist` refreshes without rebuilding.

## Cache — `.symbol/linguist.msgpack.zst`

New relative to Python (which re-classifies the whole tree every run).
msgpack + zstd level 3 (the `symbol_index` precedent), atomic tmp+rename
writes. Schema v1: `{v, root, config_fp, files: {rel_path: [mtime_ns, size,
lang_name|nil, loc, sloc]}}`. Hit = equal mtime_ns AND size → the file is
never opened; negative results (binary/empty/symlink) are cached too. Any
mismatch (version, root, config fingerprint — which covers the YAMLs, the
logic revision, and the heuristics flag) or decode error → silent cold
start. Paths not seen in the current walk are dropped on save. The language
is stored by name plus key semantics (`Language.key` = lowercased name) so a
future index/adapter consumer can apply the `registry.has_adapter` gate.

## Fidelity notes (the traps, so nobody re-trips them)

- `size` comes from stat, never content length; reads cap at 5 MiB.
- Text decoding replicates `bytes.decode(errors="ignore")` — invalid bytes
  are DROPPED, never U+FFFD (lossy decoding changes sloc).
- Line splitting replicates `str.splitlines` (11 boundary chars incl.
  `\v \f \x1c-\x1e \x85    `); blank-line test replicates
  Python `str.strip()` (`\x1c-\x1f` count as whitespace).
- `likely_binary` decodes exactly the first 1024 BYTES — a multibyte char
  cut at the boundary counts as binary, same artifact as Python.
- Extension lookup is single `splitext` (`.d.ts` matches as `.ts`).
- ModeLine ignores candidates and concatenates header+footer with NO
  separator — both bug-faithful; short files can merge lines across the
  seam exactly as Python does.
- The walk is `os.walk` semantics: dot-DIRS skipped, dot-FILES kept,
  vendor.yml pruning on forward-slash rel paths (dirs with trailing `/`),
  Python-fnmatch exclude globs with the `**/` retry. NOT gitignore-aware.
- Aggregate JSON `lines` == sloc (back-compat alias); per-file `lines` ==
  total loc. Preserved from `language_stats.py`.

## Out of scope (this branch)

Python-side integration, JSON-RPC daemon mode (lib/bin split leaves room),
CI cross-compile/wheel bundling, index/adapter consumption of the cache,
the Bayesian classifier (upstream has it; Python never did).
