import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator


PUBLIC_ROOT = Path(__file__).resolve().parents[4]


def test_compiled_demo_metadata_is_valid_and_complete():
    document = json.loads((PUBLIC_ROOT / "examples/.aimeta/meta.json").read_text())
    schema = json.loads(
        files("kx_mcp_kdbx.schemas").joinpath("aimeta-v2.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(document)

    assert {table["name"] for table in document["tables"]} == {"instruments", "trades"}
    assert document["references"] == [
        {
            "semanticType": "instrument",
            "table": "instruments",
            "keyColumn": "sym",
            "label": "name",
            "labels": ["name"],
        }
    ]
    function = document["functions"][0]
    assert function["name"] == ".analytics.vwap"
    assert function["uses"] == ["trades"]


def test_demo_source_keeps_required_aimeta_markers():
    source = (PUBLIC_ROOT / "examples/host.q").read_text()
    for marker in (
        "/ @kind data\n/ @name instruments",
        "/ @kind data\n/ @name trades",
        "/ @kind function\n/ @name .analytics.vwap",
        "/ @public",
        "/ @uses trades",
    ):
        assert marker in source
