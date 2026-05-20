"""Validate every per-package spec.toml against schemas/symbol.spec.schema.json.

Also exercises a few negative cases so the schema stays strict against the
common contributor mistakes (missing required fields, wrong key names, wrong
shape for severity-keyed maps).
"""

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCHEMA_PATH = ROOT / "schemas" / "symbol.spec.schema.json"
SPECS_DIR = ROOT / "src" / "wyolet" / "symbol" / "data" / "specs"


def _schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_spec_schema_is_valid_json():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert "name" in schema["required"]
    assert "category" in schema["required"]


def _spec_files():
    return sorted(SPECS_DIR.glob("*/spec.toml"))


def test_all_bundled_specs_match_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema)

    files = _spec_files()
    assert files, "no spec files found — wrong path?"

    failures = []
    for spec_file in files:
        raw = tomllib.loads(spec_file.read_text(encoding="utf-8"))
        for err in validator.iter_errors(raw):
            path = ".".join(str(p) for p in err.absolute_path) or "<root>"
            failures.append(f"{spec_file.parent.name}: {path}: {err.message}")

    assert not failures, "spec.toml files failed schema validation:\n" + "\n".join(failures)


@pytest.mark.parametrize(
    "raw, expected_path_fragment",
    [
        ({"category": "web"}, "name"),
        ({"name": "x"}, "category"),
        (
            {"name": "x", "category": "web", "checkers": {"orphan": {"filenames": ["a.py"]}}},
            "filenames",
        ),
        (
            {
                "name": "x",
                "category": "web",
                "checkers": {"side_effects": {"calls": {"patterns": ["foo"]}}},
            },
            "patterns",
        ),
        (
            {
                "name": "x",
                "category": "web",
                "checkers": {"side_effects": {"patterns": {"decorators": ["x"]}}},
            },
            "decorators",
        ),
        (
            {
                "name": "x",
                "category": "web",
                "checkers": {"side_effects": {"severity": "loud"}},
            },
            "loud",
        ),
    ],
)
def test_spec_schema_rejects_common_mistakes(raw, expected_path_fragment):
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(_schema())
    errors = list(validator.iter_errors(raw))
    assert errors, f"expected schema rejection, got none for {raw}"
    blob = " ".join(e.message for e in errors)
    assert expected_path_fragment in blob, f"expected mention of {expected_path_fragment!r} in {blob!r}"
