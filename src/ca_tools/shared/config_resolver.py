"""Config resolver — merge spec defaults, framework overrides, and project config."""

from ca_tools.shared.context import ActiveFramework, ResolvedConfig
from ca_tools.shared.project_config import ProjectConfig
from ca_tools.shared.spec import Spec


def resolve_config(
    spec: Spec,
    frameworks: list[ActiveFramework],
    project_config: ProjectConfig,
) -> ResolvedConfig:
    """Build a ResolvedConfig by layering spec -> frameworks -> project config."""

    # --- safe_calls: spec baseline + framework additions ---
    safe_calls = set(spec.side_effects.safe_calls)
    for fw in frameworks:
        safe_calls.update(fw.safe_calls)

    # --- known_effects: spec only (no framework/project override) ---
    known_effects = spec.side_effects.known_effects

    # --- skip_orphan_patterns: frameworks + project ignores + per-package orphan overrides ---
    skip_patterns: list[str] = []
    for fw in frameworks:
        skip_patterns.extend(fw.skip_orphan_patterns)
    skip_patterns.extend(project_config.ignore_orphans)
    for pkg_cfg in project_config.packages.values():
        skip_patterns.extend(pkg_cfg.orphan.patterns)

    # --- side effect file roles: spec baseline + framework additions ---
    file_roles: dict[str, "Severity"] = dict(spec.side_effects.file_roles)
    for fw in frameworks:
        file_roles.update(fw.file_roles)  # framework overrides spec

    # --- side effect package roles: spec baseline → per-package spec → project overrides ---
    from ca_tools.shared.findings import Severity
    package_roles: dict[str, "Severity"] = dict(spec.side_effects.package_roles)
    for pkg_name, pkg_info in spec.packages.items():
        if pkg_info.side_effects.module_level != Severity.WARNING:
            import_name = pkg_info.import_name or pkg_name.replace("-", "_")
            package_roles[import_name] = pkg_info.side_effects.module_level
    # Project-level package overrides win last
    for pkg_name, pkg_cfg in project_config.packages.items():
        if pkg_cfg.side_effects.module_level is not None:
            import_name = pkg_name.replace("-", "_")
            package_roles[import_name] = pkg_cfg.side_effects.module_level

    # --- severity overrides from project config ---
    severity_overrides: dict = {}
    severity_overrides["side_effects"] = project_config.severity_side_effects
    severity_overrides["unused_deps"] = project_config.severity_unused_deps

    # --- ignore patterns per checker ---
    ignore: dict[str, list[str]] = {}
    if project_config.ignore_side_effects:
        ignore["side_effects"] = list(project_config.ignore_side_effects)
    if project_config.ignore_deps:
        ignore["unused_deps"] = list(project_config.ignore_deps)

    # --- disabled checkers ---
    disabled = list(project_config.disabled_checkers)

    return ResolvedConfig(
        disabled_checkers=disabled,
        severity_overrides=severity_overrides,
        ignore_patterns=ignore,
        safe_calls=frozenset(safe_calls),
        known_effects=known_effects,
        skip_orphan_patterns=skip_patterns,
        side_effect_file_roles=file_roles,
        side_effect_package_roles=package_roles,
    )
