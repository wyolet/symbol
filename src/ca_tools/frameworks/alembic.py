"""Alembic framework hooks — migration files are framework-managed."""

from pathlib import Path

from ca_tools.shared.pipeline import SKIP_ORPHAN, hook


@hook(SKIP_ORPHAN, priority=40)
def skip_alembic_files(root: Path, _ctx: dict) -> list[str]:
    """Alembic env.py and migration versions are loaded by the alembic CLI, not imported."""
    alembic_ini = root / "alembic.ini"
    if not alembic_ini.exists():
        return []
    return ["alembic/env.py", "alembic/versions/*.py", "*/alembic/env.py", "*/alembic/versions/*.py"]
