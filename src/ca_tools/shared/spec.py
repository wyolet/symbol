"""Spec loader — reads spec.toml and provides typed access to all detection patterns."""

import tomllib
from dataclasses import dataclass
from importlib.resources import files

from ca_tools.shared.findings import Severity


@dataclass(frozen=True)
class PackageSideEffectsSpec:
    severity: Severity = Severity.WARNING
    calls: dict[str, frozenset[str]] = None   # severity_str → frozenset of call names
    patterns: dict[str, Severity] = None       # basename → Severity (flat)

    def __post_init__(self) -> None:
        if self.calls is None:
            object.__setattr__(self, "calls", {})
        if self.patterns is None:
            object.__setattr__(self, "patterns", {})

    @property
    def skip_calls(self) -> frozenset[str]:
        return self.calls.get("skip", frozenset())

    @property
    def error_calls(self) -> frozenset[str]:
        return self.calls.get("error", frozenset())


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
    runtime_only: bool = False
    # Glob patterns to exclude from AST analysis when this package is active.
    checker_exclude: tuple[str, ...] = ()
    # Glob patterns to exclude from LOC counting when this package is active.
    scanner_exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.side_effects is None:
            object.__setattr__(self, "side_effects", PackageSideEffectsSpec())
        if self.orphan is None:
            object.__setattr__(self, "orphan", PackageOrphanSpec())


@dataclass(frozen=True)
class SideEffectSpec:
    severity: Severity = Severity.WARNING
    calls: dict[str, frozenset[str]] = None    # severity_str → frozenset of call names
    patterns: dict[str, Severity] = None        # basename → Severity (flat)
    package_roles: dict[str, Severity] = None   # package prefix → Severity

    def __post_init__(self) -> None:
        if self.calls is None:
            object.__setattr__(self, "calls", {})
        if self.patterns is None:
            object.__setattr__(self, "patterns", {})
        if self.package_roles is None:
            object.__setattr__(self, "package_roles", {})

    @property
    def skip_calls(self) -> frozenset[str]:
        return self.calls.get("skip", frozenset())

    @property
    def known_error_calls(self) -> frozenset[str]:
        return self.calls.get("error", frozenset())



@dataclass(frozen=True)
class EntrypointSpec:
    starters: frozenset[str]
    starter_names: frozenset[str]


@dataclass(frozen=True)
class OrphanSpec:
    """Orphan checker defaults from [checkers.orphan] in spec.toml."""
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckerSpec:
    """Checker file collection defaults from [checker] in spec.toml."""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScannerSpec:
    """Scanner file collection defaults from [scanner] in spec.toml."""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class StackSpec:
    """Stack checker config from [stack] in spec.toml."""
    primary_categories: frozenset[str] = frozenset()


@dataclass(frozen=True)
class InitSpec:
    """ca init command config from [init] in spec.toml."""
    safe_side_effect_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Spec:
    categories: dict[str, str]
    packages: dict[str, PackageInfo]
    config_files: dict[str, str]
    config_dirs: dict[str, str]
    side_effects: SideEffectSpec
    entrypoints: EntrypointSpec
    checker: CheckerSpec = None  # type: ignore[assignment]
    scanner: ScannerSpec = None  # type: ignore[assignment]
    stack: StackSpec = None  # type: ignore[assignment]
    orphan: OrphanSpec = None  # type: ignore[assignment]
    init: InitSpec = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.checker is None:
            object.__setattr__(self, "checker", CheckerSpec())
        if self.scanner is None:
            object.__setattr__(self, "scanner", ScannerSpec())
        if self.stack is None:
            object.__setattr__(self, "stack", StackSpec())
        if self.orphan is None:
            object.__setattr__(self, "orphan", OrphanSpec())
        if self.init is None:
            object.__setattr__(self, "init", InitSpec())


def load_spec(
    extra_spec_paths: "list[str] | None" = None,
    project_deps: "list[str] | None" = None,
    project_root: "Path | None" = None,
) -> "Spec":
    """Load the bundled spec.toml and per-package specs, return a validated Spec.

    project_deps     — when provided, only load spec files for packages present in
                       deps (plus stdlib). Skips reading the other ~200 files entirely.
                       Pass None to load all specs (used by tests and tooling).
    extra_spec_paths — paths to additional package spec files to load after built-ins.
                       Relative paths are resolved against project_root.
                       Later entries override earlier ones (last write wins).
    project_root     — base directory for resolving relative extra_spec_paths.
    """
    from ca_tools.shared.pkg_registry import normalize_package_name

    data_root = files("ca_tools").joinpath("data")
    spec_path = data_root.joinpath("spec.toml")
    raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
    categories = dict(raw["categories"])

    dep_set: "frozenset[str] | None" = None
    if project_deps is not None:
        dep_set = frozenset(normalize_package_name(d) for d in project_deps)

    packages: dict[str, PackageInfo] = {}
    for pkg_name in raw.get("specs", {}).get("include", []):
        if dep_set is not None and normalize_package_name(pkg_name) not in dep_set:
            # Quick stdlib peek — stdlib specs are tiny (<5 lines).
            pkg_spec_path = data_root.joinpath("specs", pkg_name, "spec.toml")
            peek = tomllib.loads(pkg_spec_path.read_text(encoding="utf-8"))
            if not peek.get("stdlib", False):
                continue
            name, info = _load_package_spec(peek, categories)
            packages[name] = info
            continue

        pkg_spec_path = data_root.joinpath("specs", pkg_name, "spec.toml")
        pkg_raw = tomllib.loads(pkg_spec_path.read_text(encoding="utf-8"))
        name, info = _load_package_spec(pkg_raw, categories)
        packages[name] = info

    # Load extra specs from project config — override built-ins if same name.
    if extra_spec_paths:
        base = project_root or Path.cwd()
        for spec_str in extra_spec_paths:
            extra_path = Path(spec_str)
            if not extra_path.is_absolute():
                extra_path = base / extra_path
            if not extra_path.exists():
                import warnings
                warnings.warn(f"extra_specs path not found: {extra_path}", stacklevel=3)
                continue
            extra_raw = tomllib.loads(extra_path.read_text(encoding="utf-8"))
            name, info = _load_package_spec(extra_raw, categories)
            packages[name] = info  # intentionally overrides built-in if same name

    return _parse_spec(raw, packages)



def _load_package_spec(raw: dict, categories: dict[str, str]) -> "tuple[str, PackageInfo]":
    """Parse a root-level per-package spec file into (name, PackageInfo)."""
    pkg_name = raw["name"]
    cat = raw["category"]
    if cat not in categories:
        raise ValueError(f"Package {pkg_name!r} references unknown category {cat!r}")
    detect_raw = raw.get("detect", {})
    orphan_raw = raw.get("checkers", {}).get("orphan", raw.get("orphan", {}))
    # Support both old [side_effects] shape and new [checkers.side_effects] shape
    old_se_raw = raw.get("side_effects", {})
    new_se_raw = raw.get("checkers", {}).get("side_effects", {})
    # New shape takes precedence; fall back to old shape
    if new_se_raw:
        calls_raw = new_se_raw.get("calls", {})
        calls = {sev: frozenset(names) for sev, names in calls_raw.items()}
        patterns = _parse_roles(new_se_raw.get("patterns", {}))
        pkg_se = PackageSideEffectsSpec(
            severity=Severity(new_se_raw.get("severity", "warning")),
            calls=calls,
            patterns=patterns,
        )
    else:
        # Legacy shape — migrate fields into new structure
        _legacy_skip = list(old_se_raw.get("safe_calls", []))
        _legacy_error = list(old_se_raw.get("known_effects", []))
        calls: dict[str, frozenset[str]] = {}
        if _legacy_skip:
            calls["skip"] = frozenset(_legacy_skip)
        if _legacy_error:
            calls["error"] = frozenset(_legacy_error)
        pkg_se = PackageSideEffectsSpec(
            severity=Severity(old_se_raw.get("module_level", "warning")),
            calls=calls,
            patterns=_parse_roles(old_se_raw.get("file_roles", {})),
        )
    pkg_orphan = PackageOrphanSpec(patterns=tuple(orphan_raw.get("patterns", [])))
    info = PackageInfo(
        category=cat,
        type=raw.get("type", "lib"),
        stdlib=raw.get("stdlib", False),
        import_name=raw.get("import_name"),
        side_effects=pkg_se,
        detect_deps=frozenset(detect_raw.get("deps", [])),
        detect_config_files=frozenset(detect_raw.get("config_files", [])),
        orphan=pkg_orphan,
        runtime_only=raw.get("runtime_only", False),
        checker_exclude=tuple(raw.get("checker", {}).get("exclude", [])),
        scanner_exclude=tuple(raw.get("scanner", {}).get("exclude", [])),
    )
    return pkg_name, info


def _parse_roles(roles: dict) -> dict[str, Severity]:
    """Parse {severity: [names...]} into flat {name: Severity} for O(1) lookup."""
    result: dict[str, Severity] = {}
    for sev_str, names in roles.items():
        sev = Severity(sev_str.lower())
        for name in names:
            result[name] = sev
    return result


def _parse_spec(raw: dict, packages: "dict[str, PackageInfo]") -> "Spec":
    """Parse raw TOML dict into a validated Spec."""
    categories = dict(raw["categories"])

    ep = raw["entrypoints"]
    c = raw.get("checker", {})
    s = raw.get("scanner", {})

    # Support both old [side_effects] and new [checkers.side_effects]
    new_se = raw.get("checkers", {}).get("side_effects", {})
    old_se = raw.get("side_effects", {})
    if new_se:
        _se_calls_raw = new_se.get("calls", {})
        _se_calls = {sev: frozenset(names) for sev, names in _se_calls_raw.items()}
        _se_patterns = _parse_roles(new_se.get("patterns", {}))
        _se_pkg_roles = _parse_roles(new_se.get("package_roles", {}))
        _se_severity = Severity(new_se.get("severity", "warning"))
    else:
        _legacy_skip = list(old_se.get("safe_calls", []))
        _legacy_error = list(old_se.get("known_effects", []))
        _se_calls: dict[str, frozenset[str]] = {}
        if _legacy_skip:
            _se_calls["skip"] = frozenset(_legacy_skip)
        if _legacy_error:
            _se_calls["error"] = frozenset(_legacy_error)
        _se_patterns = _parse_roles(old_se.get("file_roles", {}))
        _se_pkg_roles = _parse_roles(old_se.get("package_roles", {}))
        _se_severity = Severity(old_se.get("severity", "warning"))

    return Spec(
        categories=categories,
        packages=packages,
        config_files=dict(raw["config_files"]),
        config_dirs=dict(raw["config_dirs"]),
        side_effects=SideEffectSpec(
            severity=_se_severity,
            calls=_se_calls,
            patterns=_se_patterns,
            package_roles=_se_pkg_roles,
        ),
        entrypoints=EntrypointSpec(
            starters=frozenset(ep["starters"]),
            starter_names=frozenset(ep["starter_names"]),
        ),
        checker=CheckerSpec(
            include=tuple(c.get("include", [])),
            exclude=tuple(c.get("exclude", [])),
        ),
        scanner=ScannerSpec(
            include=tuple(s.get("include", [])),
            exclude=tuple(s.get("exclude", [])),
        ),
        stack=StackSpec(primary_categories=frozenset(raw.get("stack", {}).get("primary_categories", []))),
        orphan=OrphanSpec(patterns=tuple(
            raw.get("checkers", {}).get("orphan", raw.get("orphan", {})).get("skip_patterns", [])
        )),
        init=InitSpec(safe_side_effect_patterns=tuple(raw.get("init", {}).get("safe_side_effect_patterns", []))),
    )
