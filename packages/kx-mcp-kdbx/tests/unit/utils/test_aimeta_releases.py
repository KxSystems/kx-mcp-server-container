"""REGRESSION: real aimeta documents, one per supported release.

`fixtures/aimeta/meta-v0.2.0.json` and `meta-v0.3.0.json` are what aimeta v0.2.0 and v0.3.0 serve
for the same annotated host (`capture-host.q` records how). The backend accepted only schemaVersion
2, so a host on the current aimeta release fell back to Tier 1 `unsupported_schema`.
"""

import json
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from kx_mcp_kdbx.utils.aimeta import classify_document, scrub_ipc_artifacts
from kx_mcp_kdbx.utils.metadata_model import _base, _visible_functions

FIXTURES = Path(__file__).parents[2] / "fixtures" / "aimeta"
RELEASES = [("0.2.0", 2, None), ("0.3.0", 3, {"action": "read", "resource": "data.trades"})]


def _captured(release):
    # The same scrub `_fetch_ipc` applies before classifying.
    return scrub_ipc_artifacts(json.loads((FIXTURES / f"meta-v{release}.json").read_text()))


@pytest.mark.parametrize("release, version, authorize", RELEASES)
def test_each_supported_release_is_tier_3_and_projects_to_contract_v1(release, version, authorize):
    detected = classify_document(_captured(release))

    assert (detected.tier, detected.annotation_status, detected.detail) == (3, "annotated", None)
    document = _base(detected)
    document["functions"] = _visible_functions(detected.document["functions"], {"trades"})
    schema = json.loads(
        files("kx_mcp_kdbx.schemas").joinpath("metadata-v1.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(document)
    assert document["metadata"]["aimetaSchemaVersion"] == version
    assert document["functions"][0].get("authorize") == authorize


def test_the_next_schema_version_is_still_refused():
    detected = classify_document({**_captured("0.3.0"), "schemaVersion": 4})

    assert (detected.tier, detected.annotation_status) == (1, "unsupported_schema")
    assert "expected 2 or 3" in (detected.detail or "")


def test_a_document_is_validated_against_its_own_version():
    # aimeta's schemas are closed: `authorize` is valid at schemaVersion 3 and unknown at 2.
    detected = classify_document({**_captured("0.3.0"), "schemaVersion": 2})

    assert detected.annotation_status == "invalid"
    assert "authorize" in (detected.detail or "")
