"""Impl tests for the table tools (mocked KDB.AI client)."""

import asyncio
from types import SimpleNamespace

from kx_mcp_kdbai.settings import KDBAIConfig
from kx_mcp_kdbai.addins import kdbai_table as mod


def test_list_tables_uses_config_database_default(mocker):
    client = mocker.Mock()
    db = mocker.Mock()
    db.tables = [SimpleNamespace(name="docs"), SimpleNamespace(name="trades")]
    client.database.return_value = db
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)

    cfg = KDBAIConfig(database_name="mydb")
    out = asyncio.run(mod.list_tables_impl(config=cfg))
    assert out == {"database": "mydb", "tables": ["docs", "trades"]}
    client.database.assert_called_once_with("mydb")


def test_table_info_success(mocker):
    client = mocker.Mock()
    table = mocker.Mock()
    table.info.return_value = {"name": "docs", "rowCount": 3}
    table.schema = [{"name": "id", "type": "long"}]
    table.indexes = []
    client.database.return_value.table.return_value = table
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)

    out = asyncio.run(mod.kdbai_table_info_impl("docs", "mydb"))
    assert out["name"] == "docs"
    assert out["schema"] == [{"name": "id", "type": "long"}]
    assert "indexes" not in out  # empty indexes are omitted


def test_table_info_error(mocker):
    mocker.patch.object(mod, "get_kdbai_client", side_effect=RuntimeError("nope"))
    out = asyncio.run(mod.kdbai_table_info_impl("docs", "mydb"))
    assert out["status"] == "error"
    assert "nope" in out["message"]
