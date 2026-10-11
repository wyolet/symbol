import json, sys, re
from pathlib import Path
from wyolet.symbol.shared.symbol_index import SymbolIndex

root = Path(sys.argv[1]); idx = SymbolIndex.load(root)
want = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
out = []
for qpath, ids in idx.by_path.items():
    for r in ids:
        k = idx.kind_of(r)
        if k not in ("function", "method", "async_function", "async_method"):
            continue
        if want and k not in want:
            continue
        try:
            sig = idx.signature(r)
        except Exception as e:
            sig = f"<err {e}>"
        # crude param parse
        m = re.search(r"\((.*)\)", sig, re.S)
        params = []
        if m:
            for p in m.group(1).split(","):
                name = p.strip().split(":")[0].split("=")[0].strip().lstrip("*")
                if name and name not in ("self", "cls", ""):
                    params.append(name)
        out.append({"qpath": qpath, "kind": k, "sig": sig.replace("\n", " ")[:160],
                    "params": params, "range": list(idx.range_of(r)),
                    "file": idx.file_of(r), "nrefs": len(idx.refs_for(r))})
Path(sys.argv[2]).write_text(json.dumps(out))
print(f"{len(out)} funcs/methods with signatures dumped")
