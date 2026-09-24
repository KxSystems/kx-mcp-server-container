"""Instrumentation for the kdb-x backend's outbound calls.

`q(conn, op, *args)` is the one qIPC seam: it issues the round-trip inside a `kdbx.<op>` span and
records its duration. `op` is a short verb from a fixed vocabulary, never client-supplied text, so
it is safe as a Prometheus label — table names, database names and query text are unbounded and
belong on spans, not on label values. See `docs/observability.md` § Backend instrumentation.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from kx_mcp_core import span
from kx_mcp_core.observability import Counter, Histogram, metric

logger = logging.getLogger(__name__)

QIPC_CALLS = metric(
    Counter,
    "kdbx_qipc_calls_total",
    "qIPC round-trips issued by the kdb-x backend, by operation and outcome.",
    ["op", "outcome"],
)
QIPC_DURATION = metric(
    Histogram,
    "kdbx_qipc_duration_seconds",
    "Wall-clock duration of a kdb-x qIPC round-trip, by operation.",
    ["op"],
)
CONNECTS = metric(
    Counter,
    "kdbx_connects_total",
    "Connection establishment attempts against KDB-X, by outcome.",
    ["outcome"],
)
RECONNECTS = metric(
    Counter,
    "kdbx_reconnects_total",
    "Times a cached KDB-X handle was found closed and re-established.",
)
SQL_ROWS = metric(
    Histogram,
    "kdbx_sql_result_rows",
    "Rows matched by a SQL query, before the response row cap is applied.",
    buckets=(1, 10, 100, 500, 1000, 5000, 50000, 500000),
)
SQL_TRUNCATED = metric(
    Counter,
    "kdbx_sql_truncated_total",
    "SQL responses truncated because the result exceeded the row cap.",
)
AUTHZ_CONSULTS = metric(
    Counter,
    "kdbx_authz_consults_total",
    "q-side authorization consults, by adapter, action and decision.",
    ["adapter", "action", "decision"],
)
EMBED_DURATION = metric(
    Histogram,
    "kdbx_embed_duration_seconds",
    "Wall-clock duration of an embedding-provider call, by provider and vector kind.",
    ["provider", "kind"],
)


@contextmanager
def timed(op: str, attributes: dict | None = None) -> Iterator[None]:
    """Time and trace one backend round-trip as `kdbx.<op>`.

    Use this for calls that are not a plain `conn(expr, ...)` — a PyKX helper method, or a block
    that issues one logical round-trip. For `conn(...)` itself prefer :func:`q`.
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        with span(f"kdbx.{op}", attributes):
            yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        _observe(QIPC_DURATION, {"op": op}, time.perf_counter() - started)
        _inc(QIPC_CALLS, {"op": op, "outcome": outcome})


def q(conn: Any, op: str, *args: Any) -> Any:
    """Issue a qIPC round-trip as `kdbx.<op>`, timed and traced."""
    with timed(op):
        return conn(*args)


@contextmanager
def embedding(provider: str, kind: str) -> Iterator[None]:
    """Time an embedding-provider call as `kdbx.embed.<kind>`.

    Separate from the vector-search round-trip on purpose: the provider may be a remote API, and
    conflating the two hides which side of a slow search is actually slow.
    """
    started = time.perf_counter()
    try:
        with span(f"kdbx.embed.{kind}", {"kdbx.embed.provider": provider}):
            yield
    finally:
        _observe(EMBED_DURATION, {"provider": provider, "kind": kind}, time.perf_counter() - started)


def record_connect(outcome: str) -> None:
    """Count a connection-establishment attempt (`ok` or `failed`)."""
    _inc(CONNECTS, {"outcome": outcome})


def record_reconnect() -> None:
    """Count a re-establish after a cached handle was found closed."""
    _inc(RECONNECTS, {})


def record_sql_result(total: int, cap: int) -> None:
    """Record the true result size of a SQL query, and whether the response was capped."""
    _observe(SQL_ROWS, {}, total)
    if total > cap:
        _inc(SQL_TRUNCATED, {})


def record_authz(adapter: str, action: str, decision: str) -> None:
    """Count an authorization consult (`allow` / `deny` / `partial`)."""
    _inc(AUTHZ_CONSULTS, {"adapter": adapter, "action": action, "decision": decision})


def _inc(collector: Any, labels: dict) -> None:
    try:
        (collector.labels(**labels) if labels else collector).inc()
    except Exception as exc:  # never let a collector chain onto the caller's error
        logger.debug("metric increment failed: %s", exc)


def _observe(collector: Any, labels: dict, value: float) -> None:
    try:
        (collector.labels(**labels) if labels else collector).observe(value)
    except Exception as exc:
        logger.debug("metric observation failed: %s", exc)
