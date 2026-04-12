"""Package name utilities and spec-based lookup."""

from ca_tools.shared.spec import Spec


def normalize_package_name(name: str) -> str:
    """Normalize a package name for registry lookup (PEP 503)."""
    return name.lower().replace("_", "-").replace(".", "-")


def lookup(package_name: str, spec: Spec) -> str | None:
    """Look up a package's category. Returns None if unknown."""
    normalized = normalize_package_name(package_name)
    if normalized in spec.packages:
        return spec.packages[normalized].category
    for suffix in ("-binary", "-python3", "-py"):
        base = normalized.removesuffix(suffix)
        if base != normalized and base in spec.packages:
            return spec.packages[base].category
    return None
