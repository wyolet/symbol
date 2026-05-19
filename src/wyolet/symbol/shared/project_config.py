"""Project-level configuration — loads [tool.symbol] or symbol.toml from target."""

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
class PackageSideEffectsOverride:
    """Project-level override for a package's side_effects checker config."""
    severity: Severity | None = None
    # backward compat alias
    @property
    def module_level(self) -> Severity | None:
        return self.severity


@dataclass
class PackageOrphanOverride:
    """Project-level override for a package's orphan checker config."""
    patterns: list[str] = field(default_factory=list)


@dataclass
class PackageProjectConfig:
    """Per-package overrides from [tool.symbol.packages.X]."""
    side_effects: PackageSideEffectsOverride = field(default_factory=PackageSideEffectsOverride)
    orphan: PackageOrphanOverride = field(default_factory=PackageOrphanOverride)


@dataclass
class SideEffectsProjectConfig:
    """Project-level overrides for [tool.symbol.checkers.side_effects].

    Wins over spec and package defaults — use to silence or add calls project-wide.
    """
    severity: Severity | None = None
    calls: dict[str, list[str]] = field(default_factory=dict)   # severity → [call names]
    patterns: dict[str, Severity] = field(default_factory=dict)  # basename → Severity


@dataclass
class CheckerProjectConfig:
    """Per-checker overrides from [tool.symbol.checkers.NAME]."""
    severity: Severity | None = None
    ignore: list[str] = field(default_factory=list)


@dataclass
class CheckerConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class ScannerConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class ProjectConfig:
    """Configuration from the target project's [tool.symbol] section."""

    checker: CheckerConfig = field(default_factory=CheckerConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    side_effects: SideEffectsProjectConfig = field(default_factory=SideEffectsProjectConfig)
    disabled_checkers: list[str] = field(default_factory=list)

    # Paths to custom checker modules — loaded before running checkers
    custom_checkers: list[str] = field(default_factory=list)

    # Paths to extra spec files — loaded after built-in specs, can add or override packages
    extra_specs: list[str] = field(default_factory=list)

    # Per-checker overrides: [tool.symbol.checkers.NAME]
    checkers: dict[str, CheckerProjectConfig] = field(default_factory=dict)

    # Per-package overrides: [tool.symbol.packages.X]
    packages: dict[str, PackageProjectConfig] = field(default_factory=dict)

    map_thresholds: MapThresholds = field(default_factory=MapThresholds)
    map_severity: MapSeverityFilter = field(default_factory=MapSeverityFilter)
    map_limit: int = 10


def load_project_config(project_root: Path) -> ProjectConfig:
    """Load symbol config from the target project.

    Discovery order (first match wins):
      1. symbol.toml at project root — standalone config, no pyproject needed
      2. [tool.symbol] in pyproject.toml
    """
    # 1. Standalone symbol.toml
    standalone = project_root / "symbol.toml"
    if standalone.exists():
        try:
            with open(standalone, "rb") as f:
                conf = tomllib.load(f)
            if conf:
                return _parse_config(conf)
        except (OSError, tomllib.TOMLDecodeError):
            pass

    # 2. pyproject.toml [tool.symbol]
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return ProjectConfig()

    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return ProjectConfig()

    conf = data.get("tool", {}).get("symbol", {})
    if not conf:
        return ProjectConfig()

    return _parse_config(conf)


def _parse_config(conf: dict) -> ProjectConfig:
    """Parse a symbol config dict into a ProjectConfig."""
    config = ProjectConfig()

    checker_raw = conf.get("checker", {})
    config.checker.include = checker_raw.get("include", [])
    config.checker.exclude = checker_raw.get("exclude", [])

    scanner_raw = conf.get("scanner", {})
    config.scanner.include = scanner_raw.get("include", [])
    config.scanner.exclude = scanner_raw.get("exclude", [])

    config.disabled_checkers = conf.get("disable", [])
    config.custom_checkers = conf.get("custom_checkers", [])
    config.extra_specs = conf.get("extra_specs", [])

    # [tool.symbol.checkers.side_effects] — project-level overrides, win over all spec layers
    # Also check legacy [tool.symbol.side_effects] for backward compat
    _checkers_se_raw = conf.get("checkers", {}).get("side_effects", {})
    _legacy_se_raw = conf.get("side_effects", {})
    se_raw = _checkers_se_raw if _checkers_se_raw else _legacy_se_raw
    if se_raw:
        if "severity" in se_raw:
            config.side_effects.severity = _parse_severity(se_raw["severity"])
        if _checkers_se_raw:
            # New shape: calls = {severity: [names]}
            calls_raw = se_raw.get("calls", {})
            config.side_effects.calls = {sev: list(names) for sev, names in calls_raw.items()}
            raw_patterns = se_raw.get("patterns", {})
            config.side_effects.patterns = {
                name: _parse_severity(sev_str)
                for sev_str, names in raw_patterns.items()
                for name in names
            }
        else:
            # Legacy shape — migrate into new calls/patterns structure
            _legacy_skip = list(se_raw.get("safe_calls", []))
            _legacy_error = list(se_raw.get("known_effects", []))
            if _legacy_skip:
                config.side_effects.calls["skip"] = _legacy_skip
            if _legacy_error:
                config.side_effects.calls["error"] = _legacy_error
            raw_roles = se_raw.get("file_roles", {})
            config.side_effects.patterns = {
                name: _parse_severity(sev_str)
                for sev_str, names in raw_roles.items()
                for name in names
            }

    # Per-checker config: [tool.symbol.checkers.NAME]
    for checker_name, checker_data in conf.get("checkers", {}).items():
        if not isinstance(checker_data, dict):
            continue
        cfg = CheckerProjectConfig()
        if "severity" in checker_data:
            cfg.severity = _parse_severity(checker_data["severity"])
        if "ignore" in checker_data:
            cfg.ignore = list(checker_data["ignore"])
        config.checkers[checker_name] = cfg

    # Map config: [tool.symbol.map]
    map_section = conf.get("map", {})
    if "limit" in map_section:
        config.map_limit = map_section["limit"]

    # Severity filter: [tool.symbol.map.severity]
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

    # Per-package overrides: [tool.symbol.packages.X]
    packages_section = conf.get("packages", {})
    for pkg_name, pkg_data in packages_section.items():
        if not isinstance(pkg_data, dict):
            continue
        pkg_cfg = PackageProjectConfig()

        se = pkg_data.get("side_effects", {})
        if isinstance(se, dict):
            if "severity" in se:
                pkg_cfg.side_effects.severity = _parse_severity(se["severity"])
            elif "module_level" in se:
                # legacy key
                pkg_cfg.side_effects.severity = _parse_severity(se["module_level"])

        orphan = pkg_data.get("orphan", {})
        if isinstance(orphan, dict) and "patterns" in orphan:
            pkg_cfg.orphan.patterns = list(orphan["patterns"])

        config.packages[pkg_name] = pkg_cfg

    return config
