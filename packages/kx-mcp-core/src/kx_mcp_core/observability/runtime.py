"""Process- and runtime-level metrics: event-loop health, thread count, build identity.

These are process facts rather than per-dispatch facts, so they do not belong in
:class:`~kx_mcp_core.observability.metrics.MetricsMiddleware`. They are collected by one background
task started lazily on the first dispatch, and they ride the same ``KX_MCP_METRICS=prometheus``
switch — no separate toggle, because they are cheap and a second knob buys an operator nothing.

**What ``prometheus-client`` already gives you, so is deliberately absent here:**
``process_cpu_seconds_total``, ``process_resident_memory_bytes``, ``process_virtual_memory_bytes``,
``process_open_fds``, ``process_max_fds`` and ``process_start_time_seconds`` from its
``ProcessCollector``, plus ``python_info`` and the ``python_gc_*`` families. They auto-register into
the default registry at import, which is the registry the scrape renders. Note ``ProcessCollector``
reads ``/proc``: it works in a Linux container and yields **nothing at all** on macOS — see
``docs/observability.md``.

**Why event-loop lag and not GIL contention.** There is no cheap, stable way to measure GIL wait
time from inside CPython, and for an asyncio server it is the wrong question: what actually degrades
this container is blocking work on the event-loop thread, whoever holds the GIL. Lag measures that
directly — schedule a wake-up, record how late it arrives — and it is the metric that makes a
blocked loop visible instead of merely slow.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from prometheus_client import Gauge, Histogram, Info

from .metrics import metric

logger = logging.getLogger(__name__)

#: How often the monitor samples. One tick a second is negligible next to a dispatch and fine
#: against Prometheus's usual 15s scrape; it is a constant rather than a setting because an operator
#: gains nothing from tuning it and every knob is surface to document.
SAMPLE_INTERVAL_SECONDS = 1.0

EVENT_LOOP_LAG = metric(
    Histogram,
    "kx_mcp_event_loop_lag_seconds",
    "How late a scheduled event-loop wake-up arrived — the delay a request would have waited "
    "behind work already on the loop.",
    # Finer than prometheus-client's default 0.005-10.0 ladder at the low end: a healthy loop sits
    # in the sub-millisecond buckets, so the default's first bucket would hide everything normal.
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0),
)

THREADS = metric(
    Gauge,
    "kx_mcp_threads",
    "Live threads in the process. Rises with the worker pools behind sync tool bodies, "
    "embedding-model inference, and outbound token minting.",
)
# Read at scrape time, not sampled: the sampler only starts on the first dispatch, so a set()-driven
# gauge would report 0 until then. `active_count` is cheap and always >= 1 (the main thread).
THREADS.set_function(threading.active_count)

BUILD_INFO = metric(
    Info,
    "kx_mcp_build",
    "Version identity of the running container, for attributing a change to a deploy.",
)

_task: Optional[asyncio.Task] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def _core_version() -> str:
    try:
        from importlib.metadata import version

        return version("kx-mcp-core")
    except Exception:  # pragma: no cover - a source checkout without metadata
        return "unknown"


def set_build_info() -> None:
    """Publish the build identity. Idempotent, and safe to call before any loop exists."""
    try:
        BUILD_INFO.info({"version": _core_version()})
    except Exception as exc:  # never let a collector break startup
        logger.debug("build info not published: %s", exc)


async def _monitor() -> None:
    """Sample event-loop lag until cancelled (the thread gauge is read at scrape time).

    Lag is the overshoot of a plain ``sleep``: ask to be woken at a known time and measure how much
    later it actually happened. Anything blocking the loop — a synchronous qIPC round-trip, model
    inference on the wrong thread, a long GC pause — shows up here as time the loop could not hand
    back, which is exactly the delay a concurrent request experiences.
    """
    loop = asyncio.get_running_loop()
    while True:
        expected = loop.time() + SAMPLE_INTERVAL_SECONDS
        try:
            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        lag = max(0.0, loop.time() - expected)
        try:
            EVENT_LOOP_LAG.observe(lag)
        except Exception as exc:  # a broken collector must not kill the monitor
            logger.debug("runtime sample failed: %s", exc)


def ensure_monitor() -> bool:
    """Start the sampler if it is not already running on this loop. Returns True if it is running.

    Called from the metrics middleware on each dispatch rather than from ``make_parent`` (which is
    sync and has no loop) or a FastMCP ``lifespan`` (a single slot, which the container claiming
    would take away from consumers writing their own glue). The cost of the "already running" path
    is one identity comparison.

    Re-keys on the loop identity so a second loop — a test, or an embedded consumer that restarts
    one — gets its own sampler instead of silently reporting from a dead one.
    """
    global _task, _loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False  # no loop yet; nothing to measure
    if _task is not None and not _task.done() and _loop is loop:
        return True
    _loop = loop
    _task = loop.create_task(_monitor())
    logger.debug("event-loop monitor started (interval %ss)", SAMPLE_INTERVAL_SECONDS)
    return True


def reset_runtime_metrics_for_tests() -> None:
    """Cancel the sampler and forget the loop it belonged to (test-only seam)."""
    global _task, _loop
    if _task is not None:
        _task.cancel()
    _task = None
    _loop = None
