"""Parity harness: Python `symbol loc --format json` vs the Rust port.

Runs both on a target tree with identical scanner excludes and diffs the
per-language integer fields (files/loc/sloc/size) exactly and percentages
within 1e-9. Gate: zero diffs when the Rust side runs --no-heuristics.

Usage: uv run scripts/linguist_parity.py <target_dir> [--heuristics]
"""

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUST_BIN = REPO / "rust/target/release/symbol-linguist"


def scanner_excludes(target: Path) -> list[str]:
    from wyolet.symbol.shared.project_config import load_project_config
    from wyolet.symbol.shared.spec import load_spec

    config = load_project_config(target)
    spec = load_spec(project_root=target)
    return list(spec.scanner.exclude) + config.scanner.exclude


def run_python(target: Path) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = subprocess.run(
        ["uv", "run", "--project", str(REPO), "symbol", "loc", str(target), "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout), time.perf_counter() - t0


def run_rust(target: Path, excludes: list[str], heuristics: bool) -> tuple[dict, float]:
    cmd = [str(RUST_BIN), str(target), "--json", "--no-cache"]
    if not heuristics:
        cmd.append("--no-heuristics")
    for e in excludes:
        cmd += ["--exclude", e]
    t0 = time.perf_counter()
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout), time.perf_counter() - t0


def main() -> int:
    target = Path(sys.argv[1]).resolve()
    heuristics = "--heuristics" in sys.argv

    excludes = scanner_excludes(target)
    py, py_t = run_python(target)
    rs, rs_t = run_rust(target, excludes, heuristics)

    py_langs = {lang["name"]: lang for lang in py["languages"]}
    rs_langs = {lang["name"]: lang for lang in rs["languages"]}

    diffs: list[str] = []
    for name in sorted(set(py_langs) | set(rs_langs)):
        a, b = py_langs.get(name), rs_langs.get(name)
        if a is None:
            diffs.append(f"{name}: only in rust ({b['files']} files, {b['sloc']} sloc)")
            continue
        if b is None:
            diffs.append(f"{name}: only in python ({a['files']} files, {a['sloc']} sloc)")
            continue
        for field in ("files", "loc", "sloc", "size"):
            if a[field] != b[field]:
                diffs.append(f"{name}.{field}: python={a[field]} rust={b[field]}")
        for field in ("percentage_lines", "percentage_bytes"):
            if abs(a[field] - b[field]) > 1e-9:
                diffs.append(f"{name}.{field}: python={a[field]} rust={b[field]}")

    for field in ("sloc", "loc", "files", "size", "languages", "code_languages"):
        if py["summary"][field] != rs["summary"][field]:
            diffs.append(f"summary.{field}: python={py['summary'][field]} rust={rs['summary'][field]}")

    mode = "heuristics ON (expected delta)" if heuristics else "no-heuristics (parity gate)"
    print(f"target: {target}")
    print(f"mode: {mode}")
    print(f"python: {py_t:.2f}s   rust: {rs_t:.3f}s")
    print(f"languages: python={len(py_langs)} rust={len(rs_langs)}")
    if diffs:
        print(f"\n{len(diffs)} DIFFS:")
        for d in diffs[:60]:
            print(f"  {d}")
        return 0 if heuristics else 1
    print("\nEXACT MATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
