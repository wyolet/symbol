"""Analysis context — shared state across all commands and checkers."""

from dataclasses import dataclass, field
from pathlib import Path

from ca_tools.shared.ast_cache import ASTCache
from ca_tools.shared.findings import Severity
from ca_tools.shared.project_config import ProjectConfig
from ca_tools.shared.spec import Spec


@dataclass(frozen=True)
class ActiveFramework:
    """A framework detected in the target project."""

    name: str
    skip_orphan_patterns: tuple[str, ...]
    safe_calls: frozenset[str]
    file_roles: dict[str, "Severity"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.file_roles is None:
            object.__setattr__(self, "file_roles", {})


@dataclass
class ResolvedConfig:
    """Layered config: spec defaults -> framework overrides -> project config."""

    disabled_checkers: list[str] = field(default_factory=list)
    severity_overrides: dict[str, Severity] = field(default_factory=dict)
    ignore_patterns: dict[str, list[str]] = field(default_factory=dict)
    safe_calls: frozenset[str] = frozenset()
    known_effects: frozenset[str] = frozenset()
    skip_orphan_patterns: list[str] = field(default_factory=list)
    # basename → Severity: merged from spec + frameworks + project
    side_effect_file_roles: dict[str, Severity] = None  # type: ignore[assignment]
    # package prefix → Severity: merged from spec + project
    side_effect_package_roles: dict[str, Severity] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.side_effect_file_roles is None:
            object.__setattr__(self, "side_effect_file_roles", {})
        if self.side_effect_package_roles is None:
            object.__setattr__(self, "side_effect_package_roles", {})


@dataclass
class AnalysisContext:
    """Shared session for all ca-tools commands.

    Holds project-wide resources (AST cache, spec, config, frameworks)
    so that commands running together share one parse pass.
    """

    project_root: Path
    spec: Spec
    config: ProjectConfig
    resolved: ResolvedConfig
    cache: ASTCache
    frameworks: list[ActiveFramework]
    deps: list[str]
    verbose: bool = False

    def has_framework(self, name: str) -> bool:
        return any(f.name == name for f in self.frameworks)

    def framework(self, name: str) -> ActiveFramework | None:
        return next((f for f in self.frameworks if f.name == name), None)


def build_context(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    verbose: bool = False,
    cache: ASTCache | None = None,
) -> AnalysisContext:
    """Build a shared AnalysisContext for any command.

    Creates AST cache, detects frameworks, resolves layered config.
    If cache is provided, reuses it (for running multiple commands together).
    """
    from ca_tools.shared.deps import detect_deps
    from ca_tools.shared.config_resolver import resolve_config
    from ca_tools.shared.framework_detector import detect_active_frameworks
    from ca_tools.shared.project_config import load_project_config
    from ca_tools.shared.registry import load_custom_checkers
    from ca_tools.shared.spec import load_spec

    spec = load_spec()
    config = load_project_config(project_root)

    if config.custom_checkers:
        load_custom_checkers(config.custom_checkers, project_root)

    inc = include or config.include or None
    exc = exclude or config.exclude or None

    if cache is None:
        cache = ASTCache(project_root, inc, exc)

    deps = detect_deps(project_root)
    frameworks = detect_active_frameworks(deps, spec, project_root)
    resolved = resolve_config(spec, frameworks, config)

    return AnalysisContext(
        project_root=project_root,
        spec=spec,
        config=config,
        resolved=resolved,
        cache=cache,
        frameworks=frameworks,
        deps=deps,
        verbose=verbose,
    )
