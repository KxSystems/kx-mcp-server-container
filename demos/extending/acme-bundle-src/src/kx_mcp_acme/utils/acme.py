"""Acme connection lifecycle — the seam a real backend fills in.

Mirrors [`kx-mcp-kdbx`'s `utils/kdbx.py`]: config is read from the request `Context` (instance-safe,
never a module global), and there is exactly **one cached connection per frozen config** so distinct
mounts get distinct connections. The connection here is an in-process **stub** so the demo runs with
no external server — a real backend replaces `_StubConnection` with its client (a pykx
`SyncQConnection`, an SDK `Session`, an `httpx` client, …) and the body of `get_connection()` with
the real dial (plus reconnect-on-closed handling).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastmcp import Context

from ..settings import AcmeConfig

logger = logging.getLogger(__name__)

# Canned data the stub "serves" instead of querying a real backend.
_CATALOG = {
    "gadget": [{"id": 1, "name": "sprocket"}, {"id": 2, "name": "flywheel"}],
    "doohickey": [{"id": 3, "name": "widget"}],
}


class _StubConnection:
    """Stand-in for a real backend client.

    Reports the endpoint it was "opened" against so you can see `ACME_DB_HOST`/`_PORT` flow through
    the config into the connection, and serves canned rows instead of querying anything.
    """

    def __init__(self, config: AcmeConfig) -> None:
        self.endpoint = f"{config.host}:{config.port}"

    def query(self, kind: str) -> dict:
        return {"endpoint": self.endpoint, "kind": kind, "rows": _CATALOG.get(kind, [])}


def config_from_ctx(ctx: Context) -> AcmeConfig:
    """Read this mount's config off the server object — never a module global (instance-safety)."""
    return getattr(ctx.fastmcp, "_acme_config")  # stashed in build_server; getattr keeps mypy happy


@lru_cache(maxsize=None)
def get_connection(config: AcmeConfig) -> _StubConnection:
    """One cached connection per frozen config (distinct mounts → distinct connections).

    A real backend dials here — e.g. ``return connect(config.host, config.port,
    timeout=config.timeout)`` — and adds transparent reconnect-on-closed. The stub just constructs.
    """
    return _StubConnection(config)


def preflight(config: AcmeConfig) -> None:
    """Eager pre-flight, run once in ``build_server()``.

    A real backend verifies reachability + the interfaces its tools need, logs one SUCCESS/ERROR
    line per check, and ``sys.exit(1)``s on hard failure (so `try_mount_bundle` disables it rather
    than serving broken). The stub always succeeds.
    """
    conn = get_connection(config)
    logger.info("Acme pre-flight: SUCCESS (stub connection to %s)", conn.endpoint)
