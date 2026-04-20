#!/usr/bin/env python3
"""Check all bundled package specs against PyPI download thresholds.

Usage:
    uv run scripts/check_pkg_thresholds.py
    uv run scripts/check_pkg_thresholds.py --threshold 50000
    uv run scripts/check_pkg_thresholds.py --json

For new PR validation (exits non-zero if package is below threshold):
    uv run scripts/check_pkg_thresholds.py --pkg loguru --strict
"""

from __future__ import annotations  # noqa: PYI001 — this script is standalone, not ca-tools code

import argparse
import json
import sys
import time
import tomllib
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

SPECS_DIR = Path(__file__).parent.parent / "src" / "wyolet.symbol" / "data" / "specs"
CORE_SPEC = Path(__file__).parent.parent / "src" / "wyolet.symbol" / "data" / "spec.toml"

THRESHOLD_DOWNLOADS = 50_000   # last-month downloads
THRESHOLD_STARS = 500          # fallback — not checked here (requires GitHub token)

# Packages that skip the download check regardless of count.
# Stdlib modules are always accepted. Audit-domain packages are strategic inclusions.
ALWAYS_ACCEPT = {
    "stdlib",           # stdlib=true in spec
    "audit-domain",     # packages directly in ca-tools' domain (bandit, safety, etc.)
}

PYPISTATS_URL = "https://pypistats.org/api/packages/{pkg}/recent"
RATE_LIMIT_DELAY = 1.0  # seconds between requests — pypistats rate-limits aggressively


def fetch_monthly_downloads(pkg: str, retries: int = 3) -> int | None:
    """Return last-month download count from pypistats, or None on error.

    Retries on 429 with exponential backoff.
    Returns None for 404 (not on PyPI) or persistent errors.
    """
    url = PYPISTATS_URL.format(pkg=pkg.lower())
    for attempt in range(retries):
        try:
            with urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            return data["data"]["last_month"]
        except HTTPError as e:
            if e.code == 404:
                return None  # not on PyPI (stdlib alias, renamed, etc.)
            if e.code == 429:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                time.sleep(wait)
                continue
            return None
        except (URLError, KeyError, json.JSONDecodeError):
            return None
    return None  # exhausted retries


def load_all_pkg_names() -> list[str]:
    """Read [specs] include from core spec.toml — canonical list of bundled packages."""
    raw = tomllib.loads(CORE_SPEC.read_text())
    return list(raw.get("specs", {}).get("include", []))


def load_pkg_spec(pkg_name: str) -> dict:
    spec_path = SPECS_DIR / pkg_name / "spec.toml"
    if not spec_path.exists():
        return {}
    return tomllib.loads(spec_path.read_text())


def is_stdlib(pkg_name: str) -> bool:
    return load_pkg_spec(pkg_name).get("stdlib", False)


def get_pypi_name(pkg_name: str) -> str:
    """Return the PyPI package name to query.

    For virtual/logical specs (e.g. django-orm) that don't exist on PyPI,
    fall back to the first detect.deps entry if present.
    """
    raw = load_pkg_spec(pkg_name)
    deps = raw.get("detect", {}).get("deps", [])
    # If the spec name itself looks like a real PyPI package, use it directly.
    # Otherwise use the first detect dep (e.g. django-orm → django).
    # We detect "virtual" specs as those with no detect.deps matching the spec name.
    if not deps or pkg_name in deps:
        return pkg_name
    return deps[0]


def check_packages(
    pkg_names: list[str],
    threshold: int,
    verbose: bool = False,
    as_json: bool = False,
) -> list[dict]:
    results = []
    for i, pkg in enumerate(pkg_names):
        if is_stdlib(pkg):
            results.append({"pkg": pkg, "status": "stdlib", "downloads": None})
            if verbose and not as_json:
                print(f"  skip  {pkg} (stdlib)")
            continue

        pypi_name = get_pypi_name(pkg)
        downloads = fetch_monthly_downloads(pypi_name)
        if i < len(pkg_names) - 1:
            time.sleep(RATE_LIMIT_DELAY)

        if downloads is None:
            status = "not-found"
        elif downloads >= threshold:
            status = "ok"
        else:
            status = "below"

        results.append({"pkg": pkg, "pypi_name": pypi_name, "status": status, "downloads": downloads})

        if not as_json:
            if status == "ok":
                if verbose:
                    print(f"  ✓  {pkg:40s}  {downloads:>12,}/month")
            elif status == "below":
                print(f"  ✗  {pkg:40s}  {downloads:>12,}/month  (threshold: {threshold:,})")
            elif status == "not-found":
                print(f"  ?  {pkg:40s}  not found on PyPI")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threshold", type=int, default=THRESHOLD_DOWNLOADS,
                        help=f"Monthly download threshold (default: {THRESHOLD_DOWNLOADS:,})")
    parser.add_argument("--pkg", help="Check a single package only")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 if any package is below threshold (for CI use)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show passing packages too")
    args = parser.parse_args()

    if args.pkg:
        pkg_names = [args.pkg]
    else:
        pkg_names = load_all_pkg_names()

    if not args.as_json:
        print(f"Checking {len(pkg_names)} packages against {args.threshold:,} downloads/month threshold...\n")

    results = check_packages(pkg_names, args.threshold, verbose=args.verbose, as_json=args.as_json)

    below = [r for r in results if r["status"] == "below"]
    not_found = [r for r in results if r["status"] == "not-found"]
    ok = [r for r in results if r["status"] == "ok"]
    stdlib_count = sum(1 for r in results if r["status"] == "stdlib")

    if args.as_json:
        print(json.dumps({
            "threshold": args.threshold,
            "total": len(results),
            "ok": len(ok),
            "below_threshold": below,
            "not_found": not_found,
            "stdlib_skipped": stdlib_count,
        }, indent=2))
    else:
        print(f"\n{'─'*60}")
        print(f"  Total checked : {len(results)}")
        print(f"  stdlib skipped: {stdlib_count}")
        print(f"  ✓ above thresh: {len(ok)}")
        print(f"  ? not on PyPI : {len(not_found)}")
        print(f"  ✗ below thresh: {len(below)}")
        if below:
            print(f"\nPackages below {args.threshold:,}/month threshold:")
            for r in sorted(below, key=lambda x: x["downloads"] or 0):
                print(f"    {r['pkg']:40s}  {r['downloads']:>10,}")
        if not_found:
            print(f"\nNot found on PyPI (stdlib aliases, renamed packages, etc.):")
            for r in not_found:
                print(f"    {r['pkg']}")

    if args.strict and (below or not_found):
        sys.exit(1)


if __name__ == "__main__":
    main()
