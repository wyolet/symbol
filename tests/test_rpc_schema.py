"""Validate the language-adapter RPC schema and ensure Python dataclasses
in protocols/types.py serialize to shapes that match it.

The schema is hand-synced with both Python (protocols/types.py) and Go
(tools/go-scan/internal/rpc/types.go). This test catches drift on the
Python side at PR time; the Go side gets its own equivalent test in
tools/go-scan/.
"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from wyolet.symbol.protocols.types import (
    FileScan,
    ParseResult,
    ScannedImport,
    ScannedRef,
    ScannedSymbol,
)

ROOT = Path(__file__).parents[1]
RPC_SCHEMA_PATH = ROOT / "schemas" / "symbol.rpc.schema.json"
RPC_METHODS_PATH = ROOT / "schemas" / "symbol.rpc.methods.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolver_for(schema: dict):
    """Build a registry pinning the schema at its declared $id so $refs resolve."""
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource

    resource = Resource.from_contents(schema)
    registry: Registry = Registry().with_resource(uri=schema["$id"], resource=resource)
    return jsonschema, registry


# ── schema validity ──────────────────────────────────────────────────


def test_rpc_schema_is_valid_jsonschema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load(RPC_SCHEMA_PATH)
    jsonschema.Draft202012Validator.check_schema(schema)
    expected_types = {
        "FileScan",
        "ScannedSymbol",
        "ScannedImport",
        "ScannedRef",
        "ParseResult",
        "ByteRange",
        "LineRange",
    }
    assert expected_types.issubset(schema["$defs"].keys())


def test_methods_doc_is_valid_jsonschema():
    jsonschema = pytest.importorskip("jsonschema")
    methods = _load(RPC_METHODS_PATH)
    jsonschema.Draft202012Validator.check_schema(methods)
    assert methods["properties"]["version"]["const"] == "1"
    declared = methods["properties"]["methods"]["properties"]
    # v1 minimum: workers must implement these.
    for required in ("initialize", "scan_file", "validate_syntax", "shutdown"):
        assert required in declared


# ── Python dataclass instances validate against their $def ──────────


def _validate(def_name: str, payload: dict) -> None:
    rpc_schema = _load(RPC_SCHEMA_PATH)
    jsonschema, registry = _resolver_for(rpc_schema)
    schema = {"$ref": f"{rpc_schema['$id']}#/$defs/{def_name}"}
    # Round-trip through JSON to match the actual wire form: tuples become
    # arrays, frozen dataclasses become objects, etc.
    wire = json.loads(json.dumps(payload))
    jsonschema.Draft202012Validator(schema, registry=registry).validate(wire)


def test_scanned_ref_matches_schema():
    ref = ScannedRef(name="foo", kind="name", line=42)
    _validate("ScannedRef", asdict(ref))


def test_scanned_import_matches_schema():
    imp = ScannedImport(local="json", source="json", line=1)
    _validate("ScannedImport", asdict(imp))


def test_scanned_symbol_matches_schema_leaf():
    sym = ScannedSymbol(
        kind="function",
        name="hello",
        qualified_path="pkg.hello",
        byte_range=(0, 50),
        line_range=(1, 5),
        refs=(ScannedRef(name="print", kind="name", line=2),),
        children=(),
    )
    _validate("ScannedSymbol", asdict(sym))


def test_scanned_symbol_matches_schema_nested():
    method = ScannedSymbol(
        kind="method",
        name="save",
        qualified_path="pkg.User.save",
        byte_range=(60, 120),
        line_range=(8, 12),
    )
    cls = ScannedSymbol(
        kind="class",
        name="User",
        qualified_path="pkg.User",
        byte_range=(40, 130),
        line_range=(6, 13),
        children=(method,),
    )
    _validate("ScannedSymbol", asdict(cls))


def test_file_scan_matches_schema_minimal_ok():
    scan = FileScan(language="python", ok=True)
    _validate("FileScan", asdict(scan))


def test_file_scan_matches_schema_error():
    scan = FileScan(language="python", ok=False, error="SyntaxError: line 3")
    _validate("FileScan", asdict(scan))


def test_file_scan_matches_schema_populated():
    scan = FileScan(
        language="go",
        ok=True,
        imports=(ScannedImport(local="fmt", source="fmt", line=3),),
        symbols=(
            ScannedSymbol(
                kind="function",
                name="main",
                qualified_path="example.main",
                byte_range=(40, 80),
                line_range=(5, 10),
            ),
        ),
    )
    _validate("FileScan", asdict(scan))


def test_parse_result_matches_schema_ok():
    _validate("ParseResult", asdict(ParseResult(ok=True)))


def test_parse_result_matches_schema_error():
    _validate(
        "ParseResult",
        asdict(ParseResult(ok=False, error_line=3, error_message="unexpected EOF")),
    )


# ── negative cases — schema rejects malformed wire data ─────────────


def test_scanned_ref_rejects_invalid_kind():
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        _validate("ScannedRef", {"name": "x", "kind": "method", "line": 1})


def test_file_scan_rejects_extra_field():
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            "FileScan",
            {"language": "python", "ok": True, "unexpected_field": "nope"},
        )
