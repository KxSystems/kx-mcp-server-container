"""REGRESSION: every qIPC round trip the backend issues is counted, checked on a real host.

The batched `meta` call went through a bare `conn(...)` instead of the instrumented `q()` helper, so
`kdbx_qipc_calls_total` never saw it and it had no span. The host counts the sync messages it
actually receives (`.z.pg`), which catches any bypass, whichever PyKX entry point it goes through,
rather than only the one call site that was wrong.
"""

import pykx as kx
import pytest
from prometheus_client import REGISTRY

from kx_mcp_kdbx.addins.kdbx_run_sql_query import run_query_impl
from kx_mcp_kdbx.utils.aimeta import MetadataCache, detect_metadata, reload_remote_metadata
from kx_mcp_kdbx.utils.kdbx import get_kdb_connection
from kx_mcp_kdbx.utils.metadata_model import build_functions_document, build_tables_document

# Three plain tables, and a message handler that records the handle of every sync request.
HOST_Q = r"""
.s.init[];
alpha:([] id:0 1 2; v:1.5 2.5 7.5);
beta:([] id:0 1; s:`x`y);
gamma:([] id:til 4; f:4?1f);
SEEN:0#0i;
.z.pg:{SEEN,:.z.w; value x};
"""


@pytest.fixture(scope="module")
def admin(host_config):
    """A second handle, whose own requests the host count leaves out."""
    conn = kx.SyncQConnection(host=host_config.host, port=host_config.port, timeout=5)
    yield conn
    conn.close()


def _received(admin) -> int:
    return int(admin("{sum SEEN<>.z.w}[]").py())


def _counted() -> dict:
    """`kdbx_qipc_calls_total` by op, summed over outcomes."""
    counts: dict = {}
    for family in REGISTRY.collect():
        if family.name != "kdbx_qipc_calls":
            continue
        for sample in family.samples:
            if sample.name.endswith("_total"):
                op = sample.labels["op"]
                counts[op] = counts.get(op, 0) + sample.value
    return counts


def _delta(before: dict, after: dict) -> dict:
    return {op: after[op] - before.get(op, 0) for op in after if after[op] != before.get(op, 0)}


def test_three_table_listing_is_eight_round_trips_with_one_meta(host_config, admin):
    cache = MetadataCache()
    build_tables_document(config=host_config, cache=cache)  # connect and fill the aimeta cache
    sent, before = _received(admin), _counted()

    document = build_tables_document(config=host_config, cache=cache)

    assert [t["name"] for t in document["tables"]] == ["alpha", "beta", "gamma"]
    ops = _delta(before, _counted())
    assert ops == {
        "probe": 1, "tables": 1, "rowcounts": 1, "partitioned": 1, "meta": 1, "preview": 3
    }
    assert _received(admin) - sent == sum(ops.values()) == 8


def test_every_round_trip_the_host_receives_is_counted(host_config, admin):
    cache = MetadataCache()
    get_kdb_connection(host_config)  # the connect itself is not a sync request
    sent, before = _received(admin), _counted()

    build_tables_document(config=host_config, cache=cache)
    build_tables_document(config=host_config, cache=cache, table="beta", preview_rows=2)
    build_functions_document(config=host_config, cache=cache)
    detect_metadata(config=host_config, cache=cache, force=True)
    reload_remote_metadata(conn=get_kdb_connection(host_config), cache=cache)
    assert run_query_impl("SELECT id FROM alpha", config=host_config)["status"] == "success"

    counted = sum(_delta(before, _counted()).values())
    assert _received(admin) - sent == counted



def _connect_overhead(host_config, admin) -> int:
    """Sync messages PyKX itself sends while opening a handle (its own capability checks)."""
    sent = _received(admin)
    kx.SyncQConnection(host=host_config.host, port=host_config.port, timeout=5).close()
    return _received(admin) - sent


def test_every_startup_round_trip_is_counted(host_config, admin):
    """The pre-flight's version, SQL and AI-libs checks used bare `conn(...)` calls, uncounted.

    The pre-flight opens one handle of its own, whose PyKX handshake is connect overhead, not a
    round trip kx-mcp issues, so it is measured and set aside rather than hard-coded.
    """
    from kx_mcp_kdbx.server import build_server
    from kx_mcp_kdbx.settings import AppSettings

    overhead = _connect_overhead(host_config, admin)
    sent, before = _received(admin), _counted()

    build_server(AppSettings(db=host_config))

    ops = _delta(before, _counted())
    assert ops.get("preflight", 0) >= 3, ops
    assert _received(admin) - sent - overhead == sum(ops.values()), ops
