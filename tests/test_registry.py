"""Tests for registry lookup."""

from ca_tools.audit.pkg_registry import lookup, normalize_package_name
from ca_tools.shared.spec import load_spec

SPEC = load_spec()


def test_normalize_basic():
    assert normalize_package_name("Flask") == "flask"
    assert normalize_package_name("my_package") == "my-package"
    assert normalize_package_name("My.Package") == "my-package"


def test_lookup_known():
    assert lookup("django", SPEC) == "web"
    assert lookup("fastapi", SPEC) == "web"
    assert lookup("sqlalchemy", SPEC) == "orm"
    assert lookup("pytest", SPEC) == "testing"
    assert lookup("openai", SPEC) == "llm"
    assert lookup("redis", SPEC) == "database_driver"


def test_lookup_case_insensitive():
    assert lookup("Django", SPEC) == "web"
    assert lookup("FastAPI", SPEC) == "web"
    assert lookup("PyTest", SPEC) == "testing"


def test_lookup_unknown():
    assert lookup("not-a-real-package-xyz", SPEC) is None


def test_lookup_suffix_stripping():
    assert lookup("psycopg2-binary", SPEC) == "database_driver"
