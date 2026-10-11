"""Fail unless pyproject.toml, Cargo.toml, the plugin's version and pins, and an optional tag agree.

Usage:
    uv run scripts/check_version.py
    uv run scripts/check_version.py --tag v0.2.5
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def cargo_version() -> str:
    return tomllib.loads((ROOT / "rust/symbol/Cargo.toml").read_text())["package"]["version"]


def plugin_versions() -> dict[str, str]:
    manifest = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())
    versions = {"plugin.json": manifest["version"]}
    for rel in ("plugin/.mcp.json", "plugin/hooks/hooks.json"):
        for n, pin in enumerate(re.findall(r"wyolet-symbol==([\w.+-]+)", (ROOT / rel).read_text())):
            versions[f"{rel} pin {n + 1}"] = pin
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="release tag, e.g. v0.2.5")
    args = parser.parse_args()

    versions = {"pyproject.toml": package_version(), "rust/symbol/Cargo.toml": cargo_version()}
    versions |= plugin_versions()
    if args.tag:
        versions[f"tag {args.tag}"] = args.tag.removeprefix("v")
    for source, version in versions.items():
        print(f"{version:<12} {source}")
    if len(set(versions.values())) != 1:
        print("error: versions differ; see CONTRIBUTING.md#releasing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
