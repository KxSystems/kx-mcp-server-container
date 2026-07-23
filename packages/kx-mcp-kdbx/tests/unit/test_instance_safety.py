"""Instance-safety: the bundle must be composable more than once in a process.

Proves the chain that lets two kdb-x backends (different host+ports) coexist under one container:
  per-instance config  ->  tool reads it from ctx.fastmcp at call time  ->  threads it down
  ->  get_kdb_connection(config)  ->  kdb_sync_connection caches per-config (distinct connections).

The discovered tool is a module-level standalone @tool function that returns the original
coroutine unchanged, so it stays directly callable here. Config is NOT captured in a closure at
registration time — it is read from the request Context (ctx.fastmcp) on each call.

No live KDB-X / multi-host demo here (that's a deployment concern) — this is the unit proof.
"""

import asyncio

from kx_mcp_kdbx.settings import KDBConfig
from kx_mcp_kdbx.utils.kdbx import kdb_sync_connection, cleanup_kdb_connection
from kx_mcp_kdbx.addins.kdbx_run_sql_query import run_sql_query


def test_distinct_configs_get_distinct_cached_connections(mocker):
    """Two backend configs -> two independently-cached connections; same config -> reused."""
    cleanup_kdb_connection()
    mock_kx = mocker.patch("kx_mcp_kdbx.utils.kdbx.kx")
    mock_kx.SyncQConnection.side_effect = lambda *a, **k: mocker.Mock()

    cfg_us = KDBConfig(host="kdb-us", port=5010)
    cfg_eu = KDBConfig(host="kdb-eu", port=5011)

    c_us = kdb_sync_connection(cfg_us)
    c_eu = kdb_sync_connection(cfg_eu)
    c_us_again = kdb_sync_connection(cfg_us)

    assert c_us is not c_eu          # distinct backends never share a connection
    assert c_us is c_us_again        # same config -> cached, no new connection
    assert mock_kx.SyncQConnection.call_count == 2
    cleanup_kdb_connection()


def test_sql_tool_routes_its_instance_config(mocker):
    """The SQL tool reads ITS server's config from the request Context (ctx.fastmcp) at call time
    and threads it down to the connection layer — never a module global."""

    class FakeServer:
        """Minimal stand-in for a FastMCP instance carrying per-instance context."""
        def __init__(self, cfg):
            self._kdbx_config = cfg

    class FakeCtx:
        """Stand-in for FastMCP's Context — ctx.fastmcp is the tool's owning (child) server."""
        def __init__(self, server):
            self.fastmcp = server

    seen = []

    def fake_get_conn(config=None):
        seen.append(config)
        raise RuntimeError("captured config; stop here")  # run_query_impl catches -> error dict

    mocker.patch(
        "kx_mcp_kdbx.addins.kdbx_run_sql_query.get_kdb_connection",
        side_effect=fake_get_conn,
    )

    cfg = KDBConfig(host="kdb-eu", port=5011)
    server = FakeServer(cfg)

    # run_sql_query is the standalone-@tool function, returned unchanged so it is still directly
    # callable. It reads cfg via config_from_ctx(ctx) -> ctx.fastmcp._kdbx_config.
    asyncio.run(run_sql_query(query="SELECT 1 FROM t", ctx=FakeCtx(server)))

    assert seen == [cfg]   # the instance's own config (via ctx.fastmcp) reached get_kdb_connection
