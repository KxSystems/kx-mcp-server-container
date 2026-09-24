"""Instrumentation for the KDB.AI backend's outbound calls.

`call(op, fn, *args, **kwargs)` is the one SDK seam: it invokes the `kdbai_client` call inside a
`kdbai.<op>` span and records its duration. `op` is a short verb from a fixed vocabulary, never
client-supplied text, so it is safe as a Prometheus label — table names, database names, index names
and query text are unbounded and belong on spans, not on label values. See
`docs/observability.md` § Backend instrumentation.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from kx_mcp_core import span
from kx_mcp_core.observability import Counter, Gauge, Histogram, metric

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

SDK_CALLS = metric(
    Counter,
    "kdbai_sdk_calls_total",
    "kdbai_client calls issued by the KDB.AI backend, by operation and outcome.",
    ["op", "outcome"],
)
SDK_DURATION = metric(
    Histogram,
    "kdbai_sdk_duration_seconds",
    "Wall-clock duration of a kdbai_client call, by operation.",
    ["op"],
)
CONNECTS = metric(
    Counter,
    "kdbai_connects_total",
    "KDB.AI session establishment attempts, by outcome.",
    ["outcome"],
)
SESSIONS = metric(
    Gauge,
    "kdbai_cached_sessions",
    "KDB.AI sessions currently cached. Grows per principal under the passthrough strategy.",
)
TOKEN_MINTS = metric(
    Counter,
    "kdbai_token_mints_total",
    "Service-account access tokens minted for KDB.AI, by outcome.",
    ["outcome"],
)
TOKEN_MINT_DURATION = metric(
    Histogram,
    "kdbai_token_mint_duration_seconds",
    "Wall-clock duration of a service-account token mint against the token endpoint.",
)
RESULT_ROWS = metric(
    Histogram,
    "kdbai_result_rows",
    "Rows returned by a KDB.AI query or search.",
    buckets=(1, 10, 50, 100, 500, 1000, 10000),
)
EMBED_DURATION = metric(
    Histogram,
    "kdbai_embed_duration_seconds",
    "Wall-clock duration of an embedding-provider call, by provider and vector kind.",
    ["provider", "kind"],
)


@contextmanager
def timed(op: str, attributes: dict | None = None) -> Iterator[None]:
    """Time and trace one KDB.AI round-trip as `kdbai.<op>`."""
    started = time.perf_counter()
    outcome = "ok"
    try:
        with span(f"kdbai.{op}", attributes):
            yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        _observe(SDK_DURATION, {"op": op}, time.perf_counter() - started)
        _inc(SDK_CALLS, {"op": op, "outcome": outcome})


def call(op: str, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Invoke a `kdbai_client` call as `kdbai.<op>`, timed and traced."""
    with timed(op):
        return fn(*args, **kwargs)


@contextmanager
def embedding(provider: str, kind: str) -> Iterator[None]:
    """Time an embedding-provider call as `kdbai.embed.<kind>`.

    Separate from the KDB.AI search round-trip on purpose: the provider may be a remote API, and
    conflating the two hides which side of a slow search is actually slow.
    """
    started = time.perf_counter()
    try:
        with span(f"kdbai.embed.{kind}", {"kdbai.embed.provider": provider}):
            yield
    finally:
        _observe(EMBED_DURATION, {"provider": provider, "kind": kind}, time.perf_counter() - started)


@contextmanager
def token_mint() -> Iterator[None]:
    """Time and trace a service-account token mint as `kdbai.token_mint`."""
    started = time.perf_counter()
    outcome = "ok"
    try:
        with span("kdbai.token_mint"):
            yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        _observe(TOKEN_MINT_DURATION, {}, time.perf_counter() - started)
        _inc(TOKEN_MINTS, {"outcome": outcome})


def record_connect(outcome: str) -> None:
    """Count a session-establishment attempt (`ok` or `failed`)."""
    _inc(CONNECTS, {"outcome": outcome})


def record_sessions(count: int) -> None:
    """Publish the current size of the session cache."""
    try:
        SESSIONS.set(count)
    except Exception as exc:
        logger.debug("metric set failed: %s", exc)


def record_result_rows(rows: int) -> None:
    """Record how many rows a query or search returned."""
    _observe(RESULT_ROWS, {}, rows)


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
