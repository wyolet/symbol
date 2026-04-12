"""FastAPI framework hooks — uvicorn string entry points."""

import re
from pathlib import Path

from ca_tools.shared.pipeline import ENTRYPOINTS, hook


@hook(ENTRYPOINTS, priority=50)
def detect_uvicorn_string_refs(project_root: Path, context: dict) -> list[str]:
    """Detect uvicorn/gunicorn module:app string references in Makefile, Dockerfile, shell scripts."""
    entry_modules: list[str] = []
    read_file = context.get("read_file")

    scan_files = ["Makefile", "Dockerfile", "Procfile"]
    for name in scan_files:
        path = project_root / name
        if not path.exists():
            continue
        content = read_file(path) if read_file else path.read_text(errors="ignore")
        if content:
            entry_modules.extend(_extract_app_refs(content))

    # Also scan shell scripts
    for sh_file in project_root.glob("scripts/*.sh"):
        content = read_file(sh_file) if read_file else sh_file.read_text(errors="ignore")
        if content:
            entry_modules.extend(_extract_app_refs(content))

    return entry_modules


def _extract_app_refs(content: str) -> list[str]:
    """Extract module paths from uvicorn/gunicorn CLI invocations."""
    refs: list[str] = []
    for match in re.finditer(r"(?:uvicorn|gunicorn)\s+([a-zA-Z0-9_.]+):", content):
        refs.append(match.group(1))
    return refs
