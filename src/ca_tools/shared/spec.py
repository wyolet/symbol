"""Spec loader — reads spec.toml and provides typed access to all detection patterns."""

import tomllib
from dataclasses import dataclass
from importlib.resources import files

from ca_tools.shared.findings import Severity


@dataclass(frozen=True)
class PackageInfo:
    category: str
    import_name: str | None = None


@dataclass(frozen=True)
class SideEffectSpec:
    safe_calls: frozenset[str]
    known_effects: frozenset[str]
    # basename → Severity: overrides default WARNING for that file
    file_roles: dict[str, "Severity"] = None  # type: ignore[assignment]
    # package prefix → Severity: overrides default WARNING for calls from that package
    package_roles: dict[str, "Severity"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.file_roles is None:
            object.__setattr__(self, "file_roles", {})
        if self.package_roles is None:
            object.__setattr__(self, "package_roles", {})


@dataclass(frozen=True)
class EntrypointSpec:
    starters: frozenset[str]
    starter_names: frozenset[str]


@dataclass(frozen=True)
class FrameworkSpec:
    name: str
    detect_deps: frozenset[str] = frozenset()
    detect_config_files: frozenset[str] = frozenset()
    skip_orphan_patterns: tuple[str, ...] = ()
    safe_calls: frozenset[str] = frozenset()
    # basename → Severity overrides for this framework's files
    file_roles: dict[str, "Severity"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.file_roles is None:
            object.__setattr__(self, "file_roles", {})


@dataclass(frozen=True)
class Spec:
    categories: dict[str, str]
    packages: dict[str, PackageInfo]
    config_files: dict[str, str]
    config_dirs: dict[str, str]
    side_effects: SideEffectSpec
    entrypoints: EntrypointSpec
    frameworks: dict[str, FrameworkSpec] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.frameworks is None:
            object.__setattr__(self, "frameworks", {})


def load_spec() -> Spec:
    """Load the bundled spec.toml and return a validated Spec."""
    spec_path = files("ca_tools").joinpath("data", "spec.toml")
    raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
    return _parse_spec(raw)


def _parse_roles(roles: dict) -> dict[str, Severity]:
    """Parse {severity: [names...]} into flat {name: Severity} for O(1) lookup."""
    result: dict[str, Severity] = {}
    for sev_str, names in roles.items():
        sev = Severity(sev_str.lower())
        for name in names:
            result[name] = sev
    return result


def _parse_spec(raw: dict) -> Spec:
    """Parse raw TOML dict into a validated Spec."""
    categories = dict(raw["categories"])

    packages: dict[str, PackageInfo] = {}
    for pkg_name, info in raw["packages"].items():
        cat = info["category"]
        if cat not in categories:
            raise ValueError(f"Package {pkg_name!r} references unknown category {cat!r}")
        packages[pkg_name] = PackageInfo(
            category=cat,
            import_name=info.get("import_name"),
        )

    se = raw["side_effects"]
    ep = raw["entrypoints"]

    # Parse frameworks (optional section)
    frameworks: dict[str, FrameworkSpec] = {}
    for fw_name, fw_data in raw.get("frameworks", {}).items():
        detect = fw_data.get("detect", {})
        fw_file_roles = _parse_roles(fw_data.get("file_roles", {}))
        frameworks[fw_name] = FrameworkSpec(
            name=fw_name,
            detect_deps=frozenset(detect.get("deps", [])),
            detect_config_files=frozenset(detect.get("config_files", [])),
            skip_orphan_patterns=tuple(fw_data.get("skip_orphan_patterns", [])),
            safe_calls=frozenset(fw_data.get("safe_calls", [])),
            file_roles=fw_file_roles,
        )

    return Spec(
        categories=categories,
        packages=packages,
        config_files=dict(raw["config_files"]),
        config_dirs=dict(raw["config_dirs"]),
        side_effects=SideEffectSpec(
            safe_calls=frozenset(se["safe_calls"]),
            known_effects=frozenset(se["known_effects"]),
            file_roles=_parse_roles(se.get("file_roles", {})),
            package_roles=_parse_roles(se.get("package_roles", {})),
        ),
        entrypoints=EntrypointSpec(
            starters=frozenset(ep["starters"]),
            starter_names=frozenset(ep["starter_names"]),
        ),
        frameworks=frameworks,
    )
