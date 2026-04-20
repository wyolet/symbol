"""Analysis context — shared state across all commands and checkers."""

from dataclasses import dataclass, field
from pathlib import Path

from wyolet.symbol.shared.ast_cache import ASTCache
from wyolet.symbol.shared.findings import Severity
from wyolet.symbol.shared.project_config import ProjectConfig
from wyolet.symbol.shared.spec import Spec


@dataclass(frozen=True)
class ActiveFramework:
    """A framework detected in the target project."""

    name: str
    skip_orphan_patterns: tuple[str, ...]
    skip_calls: frozenset[str]
    patterns: dict[str, "Severity"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.patterns is None:
            object.__setattr__(self, "patterns", {})


@dataclass
class ResolvedConfig:
    """Layered config: spec defaults -> framework overrides -> project config."""

    disabled_checkers: list[str] = field(default_factory=list)
    severity_overrides: dict[str, Severity] = field(default_factory=dict)
    ignore_patterns: dict[str, list[str]] = field(default_factory=dict)
    safe_calls: frozenset[str] = frozenset()
    error_calls: frozenset[str] = frozenset()
    skip_orphan_patterns: list[str] = field(default_factory=list)
    # basename → Severity: merged from spec + frameworks + project
    side_effect_patterns: dict[str, Severity] = None  # type: ignore[assignment]
    # package prefix → Severity: merged from spec + project
    side_effect_package_roles: dict[str, Severity] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.side_effect_patterns is None:
            object.__setattr__(self, "side_effect_patterns", {})
        if self.side_effect_package_roles is None:
            object.__setattr__(self, "side_effect_package_roles", {})



@dataclass
class AnalysisContext:
    """Shared session for all symbol commands.

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
    # Populated incrementally by runner as each checker completes.
    # Allows later checkers (e.g. orphans) to read earlier results (e.g. entrypoints).
    checker_results: dict = field(default_factory=dict)

    def has_framework(self, name: str) -> bool:
        return any(f.name == name for f in self.frameworks)

    def checker_result(self, name: str) -> list:
        """Return results from a previously-run checker, or empty list."""
        return self.checker_results.get(name, [])

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
    from wyolet.symbol.shared.deps import detect_deps
    from wyolet.symbol.shared.config_resolver import resolve_config
    from wyolet.symbol.shared.framework_detector import detect_active_frameworks
    from wyolet.symbol.shared.project_config import load_project_config
    from wyolet.symbol.shared.registry import load_custom_checkers
    from wyolet.symbol.shared.spec import load_spec

    config = load_project_config(project_root)

    if config.custom_checkers:
        load_custom_checkers(config.custom_checkers, project_root)

    # Detect deps first so load_spec only reads relevant spec files.
    deps = detect_deps(project_root)

    spec = load_spec(
        project_deps=deps,
        extra_spec_paths=config.extra_specs or None,
        project_root=project_root,
    )

    # Merge checker excludes: spec global + active package excludes + project config
    pkg_checker_excludes = [
        pat for info in spec.packages.values() for pat in info.checker_exclude
    ]
    checker_exclude = (
        list(spec.checker.exclude)
        + pkg_checker_excludes
        + config.checker.exclude
        + list(exclude or [])
    )
    checker_include = include or config.checker.include or list(spec.checker.include) or None

    if cache is None:
        cache = ASTCache(
            project_root,
            include=checker_include or None,
            exclude=checker_exclude or None,
        )

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
