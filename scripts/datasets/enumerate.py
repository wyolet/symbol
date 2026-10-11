import json, sys
from pathlib import Path
from collections import Counter, defaultdict
from wyolet.symbol.shared.symbol_index import SymbolIndex

root = Path(sys.argv[1])
idx = SymbolIndex.load(root)
assert idx is not None

rows = []
name_counter = Counter()  # leaf name -> caller-ref count via callers_of
for qpath, ids in idx.by_path.items():
    for r in ids:
        leaf = qpath.rsplit(".", 1)[-1]
        rows.append({
            "qpath": qpath,
            "leaf": leaf,
            "kind": idx.kind_of(r),
            "file": idx.file_of(r),
            "range": idx.range_of(r),
            "bases": idx.bases_for(r),
            "nrefs": len(idx.refs_for(r)),
            "lang": idx.language_of(r),
        })

# ambiguous qpaths (multi-row)
ambig = {q: len(ids) for q, ids in idx.by_path.items() if len(ids) > 1}

# callers counts per distinct leaf
leaves = set(row["leaf"] for row in rows)
caller_counts = {}
for leaf in leaves:
    caller_counts[leaf] = len(idx.callers_of(leaf))

out = {
    "n_rows": len(rows),
    "kinds": dict(Counter(r["kind"] for r in rows)),
    "rows": rows,
    "ambiguous": ambig,
    "caller_counts": caller_counts,
}
Path(sys.argv[2]).write_text(json.dumps(out, default=str))
print(f"{root.name}: {len(rows)} rows, kinds={out['kinds']}, ambiguous={len(ambig)}")
