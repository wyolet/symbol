"""Config resolver — merge spec defaults, framework overrides, and project config."""

from ca_tools.shared.context import ActiveFramework, ResolvedConfig
from ca_tools.shared.findings import Severity
from ca_tools.shared.project_config import ProjectConfig
from ca_tools.shared.spec import Spec


def resolve_config(
    spec: Spec,
    frameworks: list[ActiveFramework],
    project_config: ProjectConfig,
) -> ResolvedConfig:
    """Build a ResolvedConfig by layering spec -> frameworks -> project config."""

    # Merge order for all side_effects config:
    #   1. spec global baseline
    #   2. active package specs (additive)
    #   3. framework specs (additive, framework = detected active package)
    #   4. project [tool.ca-tools.side_effects] (wins last — full override)

    se_project = project_config.side_effects

    # --- safe_calls ---
    safe_calls = set(spec.side_effects.safe_calls)
    for pkg_info in spec.packages.values():
        safe_calls.update(pkg_info.side_effects.safe_calls)
    for fw in frameworks:
        safe_calls.update(fw.safe_calls)
    safe_calls.update(se_project.safe_calls)

    # --- known_effects ---
    known_effects: set[str] = set(spec.side_effects.known_effects)
    for pkg_info in spec.packages.values():
        known_effects.update(pkg_info.side_effects.known_effects)
    known_effects.update(se_project.known_effects)

    # --- skip_orphan_patterns ---
    skip_patterns: list[str] = []
    for fw in frameworks:
        skip_patterns.extend(fw.skip_orphan_patterns)
    orphan_cfg = project_config.checkers.get("orphans")
    if orphan_cfg:
        skip_patterns.extend(orphan_cfg.ignore)
    for pkg_cfg in project_config.packages.values():
        skip_patterns.extend(pkg_cfg.orphan.patterns)

    # --- side effect file roles: spec → packages → frameworks → project ---
    file_roles: dict[str, "Severity"] = dict(spec.side_effects.file_roles)
    for pkg_info in spec.packages.values():
        file_roles.update(pkg_info.side_effects.file_roles)
    for fw in frameworks:
        file_roles.update(fw.file_roles)
    file_roles.update(se_project.file_roles)

    # --- side effect package roles: spec baseline → per-package spec → project overrides ---
    package_roles: dict[str, Severity] = dict(spec.side_effects.package_roles)
    for pkg_name, pkg_info in spec.packages.items():
        if pkg_info.side_effects.module_level != Severity.WARNING:
            import_name = pkg_info.import_name or pkg_name.replace("-", "_")
            package_roles[import_name] = pkg_info.side_effects.module_level
    # Project-level package overrides win last
    for pkg_name, pkg_cfg in project_config.packages.items():
        if pkg_cfg.side_effects.module_level is not None:
            import_name = pkg_name.replace("-", "_")
            package_roles[import_name] = pkg_cfg.side_effects.module_level

    # --- severity overrides and ignore patterns from [tool.ca-tools.checkers.NAME] ---
    _CHECKER_DEFAULTS: dict[str, Severity] = {
        "orphans": Severity.ERROR,
        "side_effects": Severity.WARNING,
        "unused_deps": Severity.ERROR,
    }
    severity_overrides: dict = {}
    ignore: dict[str, list[str]] = {}
    for checker_name, default_sev in _CHECKER_DEFAULTS.items():
        cfg = project_config.checkers.get(checker_name)
        severity_overrides[checker_name] = cfg.severity if cfg and cfg.severity is not None else default_sev
        if cfg and cfg.ignore:
            ignore[checker_name] = list(cfg.ignore)

    # --- disabled checkers ---
    disabled = list(project_config.disabled_checkers)

    return ResolvedConfig(
        disabled_checkers=disabled,
        severity_overrides=severity_overrides,
        ignore_patterns=ignore,
        safe_calls=frozenset(safe_calls),
        known_effects=frozenset(known_effects),
        skip_orphan_patterns=skip_patterns,
        side_effect_file_roles=file_roles,
        side_effect_package_roles=package_roles,
    )
