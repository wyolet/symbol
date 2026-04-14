"""Tests for the spec loader and data integrity."""

from ca_tools.shared.spec import load_spec


def test_load_spec():
    spec = load_spec()
    assert len(spec.categories) > 0
    assert len(spec.packages) > 0
    assert len(spec.config_files) > 0
    assert len(spec.config_dirs) > 0
    assert len(spec.side_effects.skip_calls) > 0
    assert len(spec.side_effects.known_error_calls) > 0
    assert len(spec.entrypoints.starters) > 0
    assert len(spec.entrypoints.starter_names) > 0


def test_all_package_categories_exist():
    """Every package must reference a category defined in [categories]."""
    spec = load_spec()
    for pkg_name, info in spec.packages.items():
        assert info.category in spec.categories, f"Package {pkg_name!r} references unknown category {info.category!r}"


def test_no_duplicate_packages():
    """TOML keys are unique by definition, but verify load didn't lose entries."""
    spec = load_spec()
    assert len(spec.packages) >= 150, f"Expected 150+ packages, got {len(spec.packages)}"


def test_spec_is_frozen():
    import pytest

    spec = load_spec()
    with pytest.raises(AttributeError):
        spec.categories = {}  # type: ignore[misc]


def test_import_name_overrides():
    """Packages with known import name differences should have import_name set."""
    spec = load_spec()
    checks = {
        "pillow": "PIL",
        "scikit-learn": "sklearn",
        "pyjwt": "jwt",
        "beautifulsoup4": "bs4",
        "pyyaml": "yaml",
    }
    for pkg, expected_import in checks.items():
        assert pkg in spec.packages, f"Missing package {pkg!r}"
        assert spec.packages[pkg].import_name == expected_import, (
            f"Package {pkg!r} should have import_name={expected_import!r}"
        )
