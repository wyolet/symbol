"""Framework detection — identify active frameworks from declared deps and config files."""

from pathlib import Path

from ca_tools.shared.context import ActiveFramework
from ca_tools.shared.spec import Spec


def detect_active_frameworks(
    deps: list[str],
    spec: Spec,
    project_root: Path | None = None,
) -> list[ActiveFramework]:
    """Check which frameworks are active based on declared deps and config files."""
    dep_set = set(deps)
    active: list[ActiveFramework] = []

    for fw_spec in spec.frameworks.values():
        # Check dep-based detection
        dep_match = any(d in dep_set for d in fw_spec.detect_deps)

        # Check config-file-based detection
        config_match = False
        if project_root and fw_spec.detect_config_files:
            config_match = any(
                (project_root / f).exists() for f in fw_spec.detect_config_files
            )

        if dep_match or config_match:
            active.append(ActiveFramework(
                name=fw_spec.name,
                skip_orphan_patterns=fw_spec.skip_orphan_patterns,
                safe_calls=fw_spec.safe_calls,
                file_roles=fw_spec.file_roles,
            ))

    return active
