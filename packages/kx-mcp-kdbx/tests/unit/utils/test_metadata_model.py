import json
from importlib.resources import files
from unittest.mock import Mock

from jsonschema import Draft202012Validator

from kx_mcp_kdbx.utils.aimeta import MetadataDetection
from kx_mcp_kdbx.utils.metadata_model import (
    _META,
    _META_BATCH,
    MAX_PREVIEW_TABLES,
    _PREVIEW,
    _ROW_COUNTS,
    build_functions_document,
    build_tables_document,
)


class _Value:
    def __init__(self, value):
        self.value = value

    def py(self):
        return self.value


class _Tables:
    def __init__(self, names):
        self.names = names

    def __call__(self, _namespace):
        return _Value(self.names)


_META_ROWS = [{"c": "sym", "t": "s", "f": "", "a": "g"}]


class FakeConn:
    def __init__(self, names):
        self.names = names
        self.tables = _Tables(names)

    def __call__(self, query, *args):
        if query == _ROW_COUNTS:
            return _Value(json.dumps({name: 1 for name in args[0]}))
        if query == _PREVIEW:
            return _Value(json.dumps([{"sym": "AAPL"}]))
        if query == ".Q.pt":
            return _Value([])
        if query == _META:
            return _Value(json.dumps(_META_ROWS))
        if query == _META_BATCH:
            # Shape verified against real KDB-X: `{table: rows}`, and an unreadable table yields []
            # (which the caller treats as a batch miss and re-reads individually).
            return _Value(json.dumps({name: _META_ROWS for name in args[0]}))
        raise AssertionError(query)


def _annotated_document():
    return {
        "schemaVersion": 2,
        "process": {"name": "demo"},
        "tables": [
            {
                "name": "instruments",
                "private": False,
                "desc": "Reference",
                "reference": "instrument",
                "labels": ["name"],
                "columns": [
                    {"name": "sym", "kdbType": "s", "attributes": ["u"]},
                    {"name": "name", "kdbType": "s", "label": True},
                ],
            },
            {
                "name": "trades",
                "private": False,
                "desc": "Trades",
                "columns": [
                    {
                        "name": "sym",
                        "kdbType": "s",
                        "semanticType": "instrument",
                        "foreignRef": "instruments.sym",
                    }
                ],
                "sampleData": [{"sym": "MSFT"}],
            },
            {"name": "accounts", "private": False, "desc": "Accounts", "columns": []},
        ],
        "references": [
            {
                "semanticType": "instrument",
                "table": "instruments",
                "keyColumn": "sym",
                "label": "name",
                "labels": ["name"],
            }
        ],
        "functions": [
            {
                "name": ".analytics.vwap",
                "desc": "VWAP",
                "params": [],
                "returns": {"type": "table", "desc": "VWAP rows"},
                "examples": [],
                "uses": ["trades"],
                "tags": [],
            },
            {
                "name": ".admin.accounts",
                "desc": "Accounts",
                "params": [],
                "returns": {"type": "table", "desc": "Account rows"},
                "examples": [],
                "uses": ["accounts"],
                "tags": [],
            },
            {
                "name": ".util.now",
                "desc": "Clock",
                "params": [],
                "returns": {"type": "timestamp", "desc": "Current time"},
                "examples": [],
                "uses": [],
                "tags": [],
            },
        ],
    }


def _patch(mocker, names, detected):
    conn = FakeConn(names)
    mocker.patch(
        "kx_mcp_kdbx.utils.metadata_model.get_kdb_connection", return_value=conn
    )
    mocker.patch(
        "kx_mcp_kdbx.utils.metadata_model.detect_metadata", return_value=detected
    )
    mocker.patch("kx_mcp_kdbx.utils.metadata_model._vector_columns", return_value=[])
    return conn


def _validate_contract(document):
    schema = json.loads(
        files("kx_mcp_kdbx.schemas").joinpath("metadata-v1.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(document)


def test_round_trip_count_does_not_scale_linearly_with_table_count(mocker):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb-x backend).

    `columns_from_meta` and `_live_block`'s preview query were each a separate round-trip PER TABLE,
    with nothing bounding the table count — roughly `4 + U + T` sequential qIPC calls, so a host with
    500 tables meant ~1000 of them per uncached call. The meta leg is now batched into one call and
    the preview leg is capped at MAX_PREVIEW_TABLES, which bounds the total outright."""

    class _CountingConn(FakeConn):
        def __init__(self, names):
            super().__init__(names)
            self.calls = 0

        def __call__(self, query, *args):
            self.calls += 1
            return super().__call__(query, *args)

    def _run(n):
        names = [f"t{i}" for i in range(n)]
        conn = _CountingConn(names)
        mocker.patch("kx_mcp_kdbx.utils.metadata_model.get_kdb_connection", return_value=conn)
        mocker.patch(
            "kx_mcp_kdbx.utils.metadata_model.detect_metadata",
            return_value=MetadataDetection(None, 1, "unavailable", "native"),
        )
        mocker.patch("kx_mcp_kdbx.utils.metadata_model._vector_columns", return_value=[])
        build_tables_document(config=mocker.Mock(data_gate=False))
        return conn.calls

    small = _run(5)
    large = _run(50)  # 10x the tables
    assert large < small * 3, (small, large)  # must not scale ~10x with a 10x table count

    # Bounded outright, not merely sublinear: the preview cap means the count stops growing once
    # there are more tables than MAX_PREVIEW_TABLES, so 10x and 100x the tables cost the same.
    ceiling = MAX_PREVIEW_TABLES + 10  # previews + the handful of fixed calls, with slack
    assert large <= ceiling, (large, ceiling)
    assert _run(500) == large, "past the preview cap, round-trip count must stop growing"


def test_native_fallback_has_same_contract_and_live_shape(mocker):
    _patch(
        mocker,
        ["trades", "tradesstats"],
        MetadataDetection(None, 1, "unavailable", "native"),
    )

    result = build_tables_document(config=Mock(data_gate=False))

    assert [table["name"] for table in result["tables"]] == ["trades"]
    assert result["tables"][0]["columns"][0]["attributes"] == ["g"]
    assert result["tables"][0]["live"]["rowSource"] == "preview"
    _validate_contract(result)


def test_annotated_semantics_merge_with_live_rows(mocker):
    document = _annotated_document()
    _patch(
        mocker,
        ["instruments", "trades", "accounts"],
        MetadataDetection(document, 3, "annotated", "aimeta_qipc"),
    )

    result = build_tables_document(config=Mock(data_gate=False), table="trades")

    trade = result["tables"][0]
    assert trade["columns"][0]["semanticType"] == "instrument"
    assert trade["live"]["preview"] == [{"sym": "AAPL"}]
    assert "sampleData" not in trade
    assert [reference["table"] for reference in result["references"]] == ["instruments"]
    _validate_contract(result)


def test_projection_does_not_leak_fields_outside_contract_v1(mocker):
    document = _annotated_document()
    document["process"]["futureProcessField"] = "hidden"
    document["tables"][1]["futureTableField"] = "hidden"
    document["tables"][1]["columns"][0]["futureColumnField"] = "hidden"
    document["references"][0]["futureReferenceField"] = "hidden"
    document["functions"][0]["futureFunctionField"] = "hidden"
    _patch(
        mocker,
        ["instruments", "trades", "accounts"],
        MetadataDetection(document, 3, "annotated", "aimeta_qipc"),
    )

    tables = build_tables_document(config=Mock(data_gate=False))
    functions = build_functions_document(config=Mock(data_gate=False))

    trade = next(table for table in tables["tables"] if table["name"] == "trades")
    assert "futureProcessField" not in tables["metadata"]["process"]
    assert "futureTableField" not in trade
    assert "futureColumnField" not in trade["columns"][0]
    assert "futureReferenceField" not in tables["references"][0]
    assert "futureFunctionField" not in functions["functions"][0]
    _validate_contract(tables)
    _validate_contract(functions)


def test_data_gate_filters_tables_and_reference_resolvers(mocker):
    from kx_auth_core.authz import AuthzDecision

    document = _annotated_document()
    _patch(
        mocker,
        ["instruments", "trades", "accounts"],
        MetadataDetection(document, 3, "annotated", "aimeta_qipc"),
    )
    mocker.patch(
        "kx_mcp_kdbx.utils.metadata_model.consult_data_gate",
        return_value=AuthzDecision(
            allowed=True,
            obligations={"entitled": ["instruments", "trades"], "denied": ["accounts"]},
        ),
    )

    result = build_tables_document(config=Mock(data_gate=True))

    assert {table["name"] for table in result["tables"]} == {"instruments", "trades"}
    assert [reference["table"] for reference in result["references"]] == ["instruments"]
    assert result["metadata"]["hiddenByEntitlements"] == 1


def test_function_dependencies_are_filtered_by_the_same_data_gate(mocker):
    from kx_auth_core.authz import AuthzDecision

    document = _annotated_document()
    _patch(
        mocker,
        ["instruments", "trades", "accounts"],
        MetadataDetection(document, 3, "annotated", "aimeta_qipc"),
    )
    mocker.patch(
        "kx_mcp_kdbx.utils.metadata_model.consult_data_gate",
        return_value=AuthzDecision(
            allowed=True,
            obligations={"entitled": ["instruments", "trades"], "denied": ["accounts"]},
        ),
    )

    result = build_functions_document(config=Mock(data_gate=True))

    assert {fn["name"] for fn in result["functions"]} == {
        ".analytics.vwap",
        ".util.now",
    }


def test_direct_filtered_function_returns_generic_denial(mocker):
    from kx_auth_core.authz import AuthzDecision

    document = _annotated_document()
    _patch(
        mocker,
        ["instruments", "trades", "accounts"],
        MetadataDetection(document, 3, "annotated", "aimeta_qipc"),
    )
    mocker.patch(
        "kx_mcp_kdbx.utils.metadata_model.consult_data_gate",
        return_value=AuthzDecision(allowed=False, reason="denied"),
    )

    result = build_functions_document(
        config=Mock(data_gate=True), function=".admin.accounts"
    )

    assert result["status"] == "permission_denied"
    assert "accounts" not in result["message"]


def test_functions_listing_under_full_deny_matches_tables_denial(mocker):
    from kx_auth_core.authz import AuthzDecision

    document = _annotated_document()
    _patch(
        mocker,
        ["instruments", "trades", "accounts"],
        MetadataDetection(document, 3, "annotated", "aimeta_qipc"),
    )
    mocker.patch(
        "kx_mcp_kdbx.utils.metadata_model.consult_data_gate",
        return_value=AuthzDecision(allowed=False, reason="denied"),
    )

    result = build_functions_document(config=Mock(data_gate=True))

    assert result["status"] == "permission_denied"
    assert result["functions"] == []
    _validate_contract(result)


def test_stale_function_uses_is_an_error_not_a_denial(mocker):
    document = _annotated_document()
    _patch(
        mocker,
        ["instruments", "trades"],
        MetadataDetection(document, 3, "annotated", "aimeta_qipc"),
    )

    result = build_functions_document(
        config=Mock(data_gate=False), function=".admin.accounts"
    )

    assert result["status"] == "error"
    assert "accounts" in result["message"]
    _validate_contract(result)
