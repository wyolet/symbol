"""Django framework hooks — migrations and manage.py conventions."""

from pathlib import Path

from ca_tools.shared.pipeline import SKIP_ORPHAN, hook


@hook(SKIP_ORPHAN, priority=40)
def skip_django_migrations(_root: Path, ctx: dict) -> list[str]:
    """Django migration files are auto-discovered, not imported."""
    pyproject = ctx.get("pyproject", {})
    all_deps = str(pyproject.get("project", {}).get("dependencies", []))
    if "django" not in all_deps.lower():
        return []
    return ["*/migrations/*.py"]
