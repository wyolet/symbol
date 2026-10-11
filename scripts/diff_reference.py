"""Reference side of diff_runner.py. Runs on the reference checkout's
environment (`uv run --project <reference> python scripts/diff_reference.py`)
and imports the Python implementation from there; prints JSON on stdout.

  snapshot <root>...  index rows and imports of each root's saved index
  reads <root>        results of the read ops given as JSONL on stdin,
                      index loaded once
"""

import json
import sys
import time
from pathlib import Path


def snapshot(root: Path) -> dict | None:
    from wyolet.symbol.shared.symbol_index import SymbolIndex

    idx = SymbolIndex.load(root)
    if idx is None:
        return None
    rows = {}
    for qpath, ids in idx.by_path.items():
        rows[qpath] = sorted(
            (idx.file_of(r), idx.kind_of(r), idx.range_of(r),
             idx.byte_range_of(r), tuple(sorted(idx.refs_for(r))),
             tuple(idx.bases_for(r)))
            for r in ids
        )
    imports = sorted(
        (idx.files[imp[0]], idx.strings[imp[1]], idx.strings[imp[2]], imp[3])
        for imp in idx.imports if imp[1] != 0
    )
    return {"rows": rows, "imports": imports}


def read(index, op: str, args: dict):
    from wyolet.symbol.reads.callers import callers
    from wyolet.symbol.reads.code import CodeAmbiguous, CodeNotFound, code
    from wyolet.symbol.reads.outline import outline
    from wyolet.symbol.reads.search import search

    if op == "code":
        try:
            return code(index, args["target"])
        except CodeAmbiguous as e:
            return {"ambiguous": True, "candidates": e.candidates}
        except CodeNotFound as e:
            return {"error": "not_found", "message": str(e)}
    if op == "callers":
        return callers(index, args["name"])
    if op == "outline":
        return outline(index, args["query"])
    if op == "search":
        return search(
            index,
            args.get("patterns", []),
            kind=args.get("kind"),
            file=args.get("file"),
            regex=bool(args.get("regex")),
            fixed=bool(args.get("fixed")),
            ignore_case=bool(args.get("ignore_case")),
            limit=int(args.get("limit", 100)),
        )
    raise ValueError(op)


def reads(root: Path, ops: list[dict]) -> list[dict]:
    from wyolet.symbol.shared.symbol_index import get_or_build_index

    index, _ = get_or_build_index(root)
    results = []
    for o in ops:
        t0 = time.perf_counter()
        try:
            out = read(index, o["op"], o.get("args", {}))
        except Exception as e:  # the reference crashing is a finding too
            out = {"error": "exception", "message": str(e)}
        results.append({"id": o.get("id", "?"), "out": out, "dt": time.perf_counter() - t0})
    return results


def main() -> None:
    command, *rest = sys.argv[1:]
    if command == "snapshot":
        result = {root: snapshot(Path(root)) for root in rest}
    elif command == "reads":
        result = reads(Path(rest[0]), [json.loads(line) for line in sys.stdin if line.strip()])
    else:
        sys.exit(f"unknown command {command!r}")
    json.dump(result, sys.stdout, default=str)


if __name__ == "__main__":
    main()
