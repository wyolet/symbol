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
class MetricThreshold:
    """Severity tiers for a single metric — value >= threshold triggers that level."""

    info: int
    warning: int
    error: int

    def classify(self, value: int) -> Severity:
        if value >= self.error:
            return Severity.ERROR
        if value >= self.warning:
            return Severity.WARNING
        if value >= self.info:
            return Severity.INFO
        return Severity.DEBUG


@dataclass
class MapThresholds:
    """Configurable severity thresholds for map analysis metrics."""

    hotspots: MetricThreshold = field(default_factory=lambda: MetricThreshold(info=5, warning=15, error=25))
    fragile: MetricThreshold = field(default_factory=lambda: MetricThreshold(info=8, warning=15, error=25))
    deep_chains: MetricThreshold = field(default_factory=lambda: MetricThreshold(info=7, warning=12, error=18))
    cycles: MetricThreshold = field(default_factory=lambda: MetricThreshold(info=3, warning=5, error=8))


@dataclass
class MapSeverityFilter:
    """Minimum severity to display — general default with per-section overrides."""

    general: Severity = Severity.INFO
    cycles: Severity | None = None
    hotspots: Severity | None = None
    fragile: Severity | None = None
    deep_chains: Severity | None = None
    leaves: Severity | None = None

    def for_section(self, section: str) -> Severity:
        override = getattr(self, section, None)
        return override if override is not None else self.general


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

    disabled_checkers: list[str] = field(default_factory=list)

    map_thresholds: MapThresholds = field(default_factory=MapThresholds)
    map_severity: MapSeverityFilter = field(default_factory=MapSeverityFilter)
    map_limit: int = 10


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
    config.disabled_checkers = ca.get("disable", [])

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

    # Map config: [tool.ca-tools.map]
    map_section = ca.get("map", {})
    if "limit" in map_section:
        config.map_limit = map_section["limit"]

    # Severity filter: [tool.ca-tools.map.severity]
    sev_filter = map_section.get("severity", {})
    if isinstance(sev_filter, str):
        # Shorthand: severity = "warning" sets the general level
        config.map_severity.general = _parse_severity(sev_filter)
    elif isinstance(sev_filter, dict):
        if "general" in sev_filter:
            config.map_severity.general = _parse_severity(sev_filter["general"])
        for section in ("cycles", "hotspots", "fragile", "deep_chains", "leaves"):
            if section in sev_filter:
                setattr(config.map_severity, section, _parse_severity(sev_filter[section]))

    thresholds = map_section.get("thresholds", {})
    if thresholds:
        mt = config.map_thresholds
        for metric_name in ("hotspots", "fragile", "deep_chains", "cycles"):
            if metric_name in thresholds:
                vals = thresholds[metric_name]
                current = getattr(mt, metric_name)
                if "info" in vals:
                    current.info = vals["info"]
                if "warning" in vals:
                    current.warning = vals["warning"]
                if "error" in vals:
                    current.error = vals["error"]

    return config
