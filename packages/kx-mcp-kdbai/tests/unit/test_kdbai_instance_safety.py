"""Instance-safety: the kdb.ai bundle must be composable more than once in a process.

Proves the chain that lets two KDB.AI backends (different endpoints) coexist under one container:
  per-instance config -> tool reads it via ctx.fastmcp -> get_table/get_kdbai_client(config)
  -> client cache keyed per-config (distinct clients).
"""

import asyncio

from kx_mcp_kdbai.settings import KDBAIConfig
from kx_mcp_kdbai.utils.kdbai import get_kdbai_client, cleanup_kdbai_client
from kx_mcp_kdbai.addins.kdbai_database import list_databases


def test_distinct_configs_get_distinct_cached_clients(mocker):
    """Two backend configs -> two independently-cached clients; same config -> reused."""
    cleanup_kdbai_client()
    mock_session = mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())

    cfg_us = KDBAIConfig(host="kdbai-us", port=8082)
    cfg_eu = KDBAIConfig(host="kdbai-eu", port=8083)

    c_us = get_kdbai_client(cfg_us)
    c_eu = get_kdbai_client(cfg_eu)
    c_us_again = get_kdbai_client(cfg_us)

    assert c_us is not c_eu          # distinct backends never share a client
    assert c_us is c_us_again        # same config -> cached, no new session
    assert mock_session.call_count == 2
    cleanup_kdbai_client()


def test_tool_routes_its_instance_config(mocker):
    """A standalone tool reads ITS server's config from ctx.fastmcp and threads it to the client layer."""

    class FakeServer:
        def __init__(self, cfg):
            self._kdbai_config = cfg

    class FakeCtx:
        def __init__(self, server):
            self.fastmcp = server

    seen = []

    def fake_get_client(config=None):
        seen.append(config)
        raise RuntimeError("captured config; stop here")  # impl catches -> error dict

    mocker.patch("kx_mcp_kdbai.addins.kdbai_database.get_kdbai_client", side_effect=fake_get_client)

    cfg = KDBAIConfig(host="kdbai-eu", port=8083)
    server = FakeServer(cfg)

    # The standalone @tool decorator returns the function unchanged, so it is directly callable.
    asyncio.run(list_databases(ctx=FakeCtx(server)))

    assert seen == [cfg]   # the instance's own config (via ctx.fastmcp) reached get_kdbai_client
