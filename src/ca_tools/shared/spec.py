"""Spec loader — reads spec.toml and provides typed access to all detection patterns."""

import tomllib
from dataclasses import dataclass
from importlib.resources import files

from ca_tools.shared.findings import Severity


@dataclass(frozen=True)
class PackageSideEffectsSpec:
    module_level: Severity = Severity.WARNING
    safe_calls: frozenset[str] = frozenset()
    # basename → Severity overrides for this package's framework files
    file_roles: dict[str, "Severity"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.file_roles is None:
            object.__setattr__(self, "file_roles", {})


@dataclass(frozen=True)
class PackageOrphanSpec:
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageInfo:
    category: str
    type: str = "lib"              # lib | tool | app
    stdlib: bool = False
    import_name: str | None = None
    side_effects: PackageSideEffectsSpec = None  # type: ignore[assignment]
    detect_deps: frozenset[str] = frozenset()
    detect_config_files: frozenset[str] = frozenset()
    orphan: PackageOrphanSpec = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.side_effects is None:
            object.__setattr__(self, "side_effects", PackageSideEffectsSpec())
        if self.orphan is None:
            object.__setattr__(self, "orphan", PackageOrphanSpec())


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
class Spec:
    categories: dict[str, str]
    packages: dict[str, PackageInfo]
    config_files: dict[str, str]
    config_dirs: dict[str, str]
    side_effects: SideEffectSpec
    entrypoints: EntrypointSpec


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
        detect_raw = info.get("detect", {})
        orphan_raw = info.get("orphan", {})
        se_raw = info.get("side_effects", {})
        pkg_se = PackageSideEffectsSpec(
            module_level=Severity(se_raw.get("module_level", "warning")),
            safe_calls=frozenset(se_raw.get("safe_calls", [])),
            file_roles=_parse_roles(se_raw.get("file_roles", {})),
        )
        pkg_orphan = PackageOrphanSpec(patterns=tuple(orphan_raw.get("patterns", [])))
        packages[pkg_name] = PackageInfo(
            category=cat,
            type=info.get("type", "lib"),
            stdlib=info.get("stdlib", False),
            import_name=info.get("import_name"),
            side_effects=pkg_se,
            detect_deps=frozenset(detect_raw.get("deps", [])),
            detect_config_files=frozenset(detect_raw.get("config_files", [])),
            orphan=pkg_orphan,
        )

    se = raw["side_effects"]
    ep = raw["entrypoints"]

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
    )
