"""Project-level configuration — loads [tool.ca-tools] from target's pyproject.toml."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .findings import Severity


def _parse_severity(value: str) -> Severity:
    try:
        return Severity(value.lower())
    except ValueError:
        valid = ", ".join(s.value for s in Severity)
        raise ValueError(f"Invalid severity {value!r}, must be one of: {valid}") from None


@dataclass
class ProjectConfig:
    """Configuration from the target project's [tool.ca-tools] section."""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    severity_orphans: Severity = Severity.ERROR
    severity_side_effects: Severity = Severity.WARNING
    severity_unused_deps: Severity = Severity.ERROR

    ignore_deps: list[str] = field(default_factory=list)
    ignore_orphans: list[str] = field(default_factory=list)
    ignore_side_effects: list[str] = field(default_factory=list)


def load_project_config(project_root: Path) -> ProjectConfig:
    """Load [tool.ca-tools] from the target project's pyproject.toml."""
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return ProjectConfig()

    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return ProjectConfig()

    ca = data.get("tool", {}).get("ca-tools", {})
    if not ca:
        return ProjectConfig()

    config = ProjectConfig()

    config.include = ca.get("include", [])
    config.exclude = ca.get("exclude", [])

    severity = ca.get("severity", {})
    if "orphans" in severity:
        config.severity_orphans = _parse_severity(severity["orphans"])
    if "side_effects" in severity:
        config.severity_side_effects = _parse_severity(severity["side_effects"])
    if "unused_deps" in severity:
        config.severity_unused_deps = _parse_severity(severity["unused_deps"])

    ignore = ca.get("ignore", {})
    config.ignore_deps = ignore.get("deps", [])
    config.ignore_orphans = ignore.get("orphans", [])
    config.ignore_side_effects = ignore.get("side_effects", [])

    return config
