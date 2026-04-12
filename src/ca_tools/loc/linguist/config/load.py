import os
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
