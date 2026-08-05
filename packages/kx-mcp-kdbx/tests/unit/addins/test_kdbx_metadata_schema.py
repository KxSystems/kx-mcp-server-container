import json

from kx_mcp_kdbx.addins.kdbx_metadata_schema import kdbx_metadata_schema_impl


def test_schema_resource_is_contract_v1():
    schema = json.loads(kdbx_metadata_schema_impl())
    assert schema["$id"] == "schema://kdbx/metadata/v1"
    assert schema["properties"]["schemaVersion"]["const"] == 1
    assert schema["$defs"]["table"]["additionalProperties"] is False
    assert schema["$defs"]["column"]["additionalProperties"] is False
    assert schema["$defs"]["reference"]["additionalProperties"] is False
    assert schema["$defs"]["function"]["additionalProperties"] is False
    assert schema["$defs"]["function"]["properties"]["params"]["items"] == {
        "$ref": "#/$defs/param"
    }
