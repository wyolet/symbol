"""Framework detection — identify active frameworks from declared deps and config files."""

from pathlib import Path

from wyolet.symbol.shared.context import ActiveFramework
from wyolet.symbol.shared.spec import Spec


def detect_active_frameworks(
    deps: list[str],
    spec: Spec,
    project_root: Path | None = None,
) -> list[ActiveFramework]:
    """Check which packages act as frameworks (have detect config) and are active."""
    dep_set = set(deps)
    active: list[ActiveFramework] = []

    for pkg_name, pkg_info in spec.packages.items():
        if not pkg_info.detect_deps and not pkg_info.detect_config_files:
            continue  # not a framework package

        dep_match = any(d in dep_set for d in pkg_info.detect_deps)

        config_match = False
        if project_root and pkg_info.detect_config_files:
            config_match = any(
                (project_root / f).exists() for f in pkg_info.detect_config_files
            )

        if dep_match or config_match:
            active.append(ActiveFramework(
                name=pkg_name,
                skip_orphan_patterns=pkg_info.orphan.patterns,
                skip_calls=pkg_info.side_effects.skip_calls,
                patterns=pkg_info.side_effects.patterns,
            ))

    return active
