"""Spec loader — reads spec.toml and provides typed access to all detection patterns."""

import tomllib
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class PackageInfo:
    category: str
    import_name: str | None = None


@dataclass(frozen=True)
class SideEffectSpec:
    safe_calls: frozenset[str]
    known_effects: frozenset[str]


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

    return Spec(
        categories=categories,
        packages=packages,
        config_files=dict(raw["config_files"]),
        config_dirs=dict(raw["config_dirs"]),
        side_effects=SideEffectSpec(
            safe_calls=frozenset(se["safe_calls"]),
            known_effects=frozenset(se["known_effects"]),
        ),
        entrypoints=EntrypointSpec(
            starters=frozenset(ep["starters"]),
            starter_names=frozenset(ep["starter_names"]),
        ),
    )
