"""Connection-layer helpers: config_from_ctx routing + get_table config threading."""

from kx_mcp_kdbai.utils import kdbai as mod
from kx_mcp_kdbai.settings import KDBAIConfig


def test_config_from_ctx_reads_instance_config():
    class Server:
        _kdbai_config = KDBAIConfig(host="kdbai-eu")

    class Ctx:
        fastmcp = Server()

    assert mod.config_from_ctx(Ctx()).host == "kdbai-eu"


def test_config_from_ctx_missing_returns_none():
    class Ctx:
        fastmcp = object()

    assert mod.config_from_ctx(Ctx()) is None


def test_get_table_threads_config(mocker):
    client = mocker.Mock()
    get_client = mocker.patch.object(mod, "get_kdbai_client", return_value=client)

    cfg = KDBAIConfig(database_name="mydb")
    mod.get_table("docs", config=cfg)

    get_client.assert_called_once_with(cfg)
    client.database.assert_called_once_with("mydb")
    client.database.return_value.table.assert_called_once_with("docs")
