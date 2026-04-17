"""Config file detection — find infrastructure and configuration files."""

from dataclasses import dataclass
from pathlib import Path

from ca.symbol.shared.spec import Spec


@dataclass
class ConfigFile:
    path: Path
    description: str


def detect_config_files(project_root: Path, spec: Spec) -> list[ConfigFile]:
    results: list[ConfigFile] = []
    seen: set[str] = set()

    for filename, description in spec.config_files.items():
        filepath = project_root / filename
        if filepath.exists() and filename not in seen:
            seen.add(filename)
            results.append(ConfigFile(path=filepath, description=description))

    for dirname, description in spec.config_dirs.items():
        dirpath = project_root / dirname
        if dirpath.exists():
            display_name = dirname + ("/" if dirpath.is_dir() else "")
            if display_name not in seen:
                seen.add(display_name)
                results.append(ConfigFile(path=dirpath, description=description))

    return results
