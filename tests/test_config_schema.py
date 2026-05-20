"""Tests for the published project configuration schema."""

import json
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "symbol.config.schema.json"


def test_config_schema_is_valid_json():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"


def test_config_schema_covers_project_config_sections():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    properties = schema["properties"]

    assert {"checker", "scanner", "checkers", "packages", "map"} <= set(properties)

    checkers = properties["checkers"]["properties"]
    assert {"orphans", "side_effects", "unused_deps"} <= set(checkers)


def test_config_schema_accepts_documented_config_shape():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    config = {
        "checker": {"exclude": ["alembic/*", "scripts/*"]},
        "checkers": {
            "orphans": {
                "severity": "warning",
                "ignore": ["alembic/*", "src/main.py"],
            },
            "side_effects": {
                "severity": "info",
                "ignore": ["*.include_router()", "*.add_middleware()"],
            },
            "unused_deps": {
                "severity": "error",
                "ignore": ["greenlet", "psycopg"],
            },
        },
    }

    jsonschema.validate(config, schema)
