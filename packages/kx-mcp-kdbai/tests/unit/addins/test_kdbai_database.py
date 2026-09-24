"""Impl tests for the database tools (mocked KDB.AI client)."""

from types import SimpleNamespace

from kx_mcp_kdbai.addins import kdbai_database as mod


def test_list_databases_success(mocker):
    client = mocker.Mock()
    client.databases.return_value = [SimpleNamespace(name="default"), SimpleNamespace(name="docs")]
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)

    out = mod.kdbai_list_databases_impl()
    assert out == {"status": "success", "databases": ["default", "docs"]}


def test_list_databases_error(mocker):
    mocker.patch.object(mod, "get_kdbai_client", side_effect=RuntimeError("boom"))
    out = mod.kdbai_list_databases_impl()
    assert out["status"] == "error"
    assert "boom" in out["message"]


def test_databases_info_specific(mocker):
    client = mocker.Mock()
    client.database.return_value.info.return_value = {"tables": [{"name": "t"}]}
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)

    out = mod.kdbai_databases_info_impl("default")
    assert out["status"] == "success"
    assert out["info"] == {"tables": [{"name": "t"}]}
    client.database.assert_called_once_with("default")


def test_databases_info_all(mocker):
    client = mocker.Mock()
    client.databases_info.return_value = {"databases": []}
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)

    out = mod.kdbai_databases_info_impl(None)
    assert out["status"] == "success"
    client.databases_info.assert_called_once()
