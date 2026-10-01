"""REGRESSION: a table preview over every float edge a q host emits, on a real host.

q's `.j.j` writes +/-infinity as bare `inf`, which `json.loads` rejects. The preview swallowed that
failure and reported `rowSource: "none"`, which reads as "past the preview cap", so a table asked for
by name silently lost its rows. A mocked `.j.j` payload cannot show this; only a real host can.
"""

import json
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator

from kx_mcp_kdbx.utils.metadata_model import build_tables_document

# One table per edge, float (9h) and real (8h). `nested` holds infinity inside a list cell, which the
# projection does not reach. `keyed` must preview like any other table. `failing` makes the host's own
# serializer signal (a wrapped `.j.j`), a real preview failure that is not about floats.
HOST_Q = r"""
fposinf:([] id:0 1; f:1.5 0w);
fneginf:([] id:0 1; f:1.5 -0w);
fnull:([] id:0 1; f:1.5 0n);
eposinf:([] id:0 1; e:"e"$1.5 0w);
eneginf:([] id:0 1; e:"e"$1.5 -0w);
enull:([] id:0 1; e:1.5 0Ne);
nested:([] id:0 1; v:(1 2f;0w 1f));
keyed:([id:0 1] f:1.5 2.5);
failing:([] id:0 1; boom:1 2);
.j.j0:.j.j; .j.j:{$[(98h=type x) and `boom in cols x;'`boom;.j.j0 x]};
"""


def _validate(document):
    schema = json.loads(
        files("kx_mcp_kdbx.schemas").joinpath("metadata-v1.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(document)


def _live(config, table):
    document = build_tables_document(config=config, table=table, preview_rows=2)
    _validate(document)
    assert document["status"] == "success", document
    return document["tables"][0]["live"]


@pytest.mark.parametrize(
    "table, column, projected",
    [
        ("fposinf", "f", True),
        ("fneginf", "f", True),
        ("fnull", "f", False),  # a q null is already JSON null: no projection, no flag
        ("eposinf", "e", True),
        ("eneginf", "e", True),
        ("enull", "e", False),
    ],
)
def test_preview_renders_every_float_edge(host_config, table, column, projected):
    live = _live(host_config, table)

    assert live["rowSource"] == "preview", live
    assert live["preview"] == [{"id": 0, column: 1.5}, {"id": 1, column: None}]
    assert live.get("nonFiniteAsNull", False) is projected
    assert "previewError" not in live


def test_unprojectable_infinity_is_reported_not_swallowed(host_config):
    live = _live(host_config, "nested")

    assert live["rowSource"] == "error"
    assert live["previewError"]["code"] == "non_finite_number"
    assert "preview" not in live


def test_a_keyed_table_previews(host_config):
    live = _live(host_config, "keyed")

    assert live["rowSource"] == "preview", live
    assert live["preview"] == [{"id": 0, "f": 1.5}, {"id": 1, "f": 2.5}]


def test_any_other_preview_failure_is_reported(host_config):
    live = _live(host_config, "failing")

    assert live["rowSource"] == "error"
    assert live["previewError"]["code"] == "preview_failed"
    assert live["previewError"]["message"]
