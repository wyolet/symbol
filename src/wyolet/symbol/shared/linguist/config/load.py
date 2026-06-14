import os
import re
from functools import cache
from typing import Literal
from urllib.request import urlopen

import yaml

ConfigType = Literal["documentation", "vendor", "popular", "languages", "heuristics", "generic"]

CONFIG_DIR = os.path.dirname(__file__)

LINGUIST_BASE_URL = "https://raw.githubusercontent.com/github-linguist/linguist/refs/heads/main/lib/linguist/"

CONFIG_FILES = ["documentation.yml", "vendor.yml", "popular.yml", "languages.yml", "heuristics.yml", "generic.yml"]


@cache
def load_config(config: ConfigType):
    with open(os.path.join(CONFIG_DIR, f"{config}.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


# Never-matching regex: used as the fallback when no vendor pattern compiles.
_NEVER = re.compile(r"(?!)")


@cache
def load_vendor_regex() -> "re.Pattern[str]":
    """Compile ``vendor.yml`` into one combined regex matched in a single pass.

    ``vendor.yml`` ships ~168 Ruby-flavored path patterns (``(^|/)node_modules/``,
    ``(^|/)vendors?/``, …). Running 168 ``re.search`` calls per path across a
    large tree is the wrong primitive — we fold them into a single alternation
    compiled once. Patterns that aren't valid Python regex (rare Ruby-isms) are
    skipped rather than failing the whole match.

    Paths are matched verbatim against the patterns, so callers should pass
    forward-slash, repo-relative paths (directories with a trailing ``/``) the
    way GitHub Linguist's own ``vendored?`` check does.
    """
    valid: list[str] = []
    for pattern in load_config("vendor"):
        try:
            re.compile(pattern)
        except re.error:
            continue
        valid.append(f"(?:{pattern})")
    return re.compile("|".join(valid)) if valid else _NEVER


def update_from_github(callback=None):
    """Download latest linguist config files from GitHub.

    Args:
        callback: optional function called with (filename, status) for progress reporting.
    """
    for filename in CONFIG_FILES:
        try:
            url = LINGUIST_BASE_URL + filename
            with urlopen(url, timeout=30) as resp:  # noqa: S310
                data = resp.read()
            dest = os.path.join(CONFIG_DIR, filename)
            with open(dest, "wb") as f:
                f.write(data)
            if callback:
                callback(filename, "ok")
        except Exception as e:
            if callback:
                callback(filename, f"error: {e}")
            else:
                raise

    # Clear cached configs so next load picks up new data
    load_config.cache_clear()
