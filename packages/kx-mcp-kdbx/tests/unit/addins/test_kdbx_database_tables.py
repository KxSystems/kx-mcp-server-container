"""Tests for the table metadata resources."""

import json

import pytest

from kx_mcp_kdbx.addins.kdbx_database_tables import (
    kdbx_describe_table_impl,
    kdbx_describe_tables_impl,
)


@pytest.mark.anyio
async def test_describe_tables_serializes_contract_document(mocker):
    document = {"schemaVersion": 1, "status": "success", "tables": []}
    build = mocker.patch(
        "kx_mcp_kdbx.addins.kdbx_database_tables.build_tables_document",
        return_value=document,
    )

    result = kdbx_describe_tables_impl(config="config", cache="cache")

    assert json.loads(result[0].text) == document
    build.assert_called_once_with(config="config", cache="cache")


@pytest.mark.anyio
async def test_describe_one_table_threads_preview_and_instance_state(mocker):
    document = {"schemaVersion": 1, "status": "success", "tables": [{"name": "trades"}]}
    build = mocker.patch(
        "kx_mcp_kdbx.addins.kdbx_database_tables.build_tables_document",
        return_value=document,
    )

    result = kdbx_describe_table_impl(
        "trades", config="config", cache="cache", preview_rows=7
    )

    assert json.loads(result[0].text)["tables"][0]["name"] == "trades"
    build.assert_called_once_with(
        config="config", cache="cache", table="trades", preview_rows=7
    )


@pytest.mark.anyio
async def test_resource_error_is_clean_json(mocker):
    mocker.patch(
        "kx_mcp_kdbx.addins.kdbx_database_tables.build_tables_document",
        side_effect=RuntimeError("connection failed"),
    )

    result = kdbx_describe_tables_impl()

    document = json.loads(result[0].text)
    assert document["status"] == "error"
    assert document["message"] == "connection failed"
    assert document["$schema"] == "schema://kdbx/metadata/v1"
