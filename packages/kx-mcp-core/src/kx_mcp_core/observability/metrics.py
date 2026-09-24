"""Prometheus metrics: count and time every tool / resource / prompt dispatch.

Rides the same parent ``add_middleware`` seam as :class:`~kx_mcp_core.auth.AuditMiddleware`, so one
middleware on the parent observes **every** mounted backend's dispatches with no per-backend wiring.
By the time a dispatch reaches parent middleware the target name is already namespaced
(``kdbx_run_sql_query`` / ``kdbai_query_data``), so metrics split per backend for free.

The scrape endpoint is a plain HTTP route on the container's *own* ASGI app
(:func:`mount_metrics_route`) — not a second server and not a second port. It therefore only exists
under an HTTP transport; under ``stdio`` there is no listener at all, so the mount degrades to a
warning (the ``try_mount_bundle`` "never crash the container" posture).

**Why module-level collectors.** Everywhere else in this codebase module-level state is a bug (it
silently couples every mount — see the instance-safety contract in ``docs/extending.md``). Metrics
are the documented exception: Prometheus's registry is *process*-global by design, and a scrape must
see one set of cumulative counters for the process, not one per mounted backend. The per-backend
dimension is carried by the ``tool_name`` label, not by separate collector instances.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence, Type, TypeVar

from fastmcp.server.middleware import Middleware, MiddlewareContext
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest
from prometheus_client.metrics import MetricWrapperBase

from ._outcome import (
    OUTCOME_DENIED,
    OUTCOME_ERROR,
    OUTCOME_OK,
    outcome_for_exception,
    outcome_for_result,
)
from ._target import target_name, target_uri

logger = logging.getLogger(__name__)

# The transports that actually serve HTTP, and so can host the scrape route. Mirrors the launcher's
# --transport choices minus "stdio" (which has no network listener).
HTTP_TRANSPORTS = ("streamable-http", "http")

# The outcome label values are the same three-way split AuditMiddleware records, classified by the
# shared `_outcome` helper so a metric, a span, and an audit line never disagree. Re-exported here
# because they are part of this module's documented surface.
__all__ = [
    "DISPATCH_COUNTER",
    "DISPATCH_DURATION",
    "DISPATCH_IN_PROGRESS",
    "HTTP_TRANSPORTS",
    "MetricsMiddleware",
    "OUTCOME_DENIED",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "metric",
    "mount_metrics_route",
]


_TOTAL_SUFFIX = "_total"

_M = TypeVar("_M", bound=MetricWrapperBase)


def metric(
    cls: Type[_M],
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    **kwargs: Any,
) -> _M:
    """Define a Prometheus collector idempotently. **The seam for bundle authors.**

    ``metric(Counter, "acme_widgets_enriched_total", "Widgets enriched.", ["kind"])`` — ``name`` is
    the name that appears in the ``/metrics`` scrape, and it is the only place that name is written.
    Anything registered anywhere in the process lands in the container's own scrape, so a bundle needs
    no route work and no registration call.

    ``cls`` is the collector class (``Counter``, ``Gauge``, ``Histogram``, ``Summary``, ``Info``,
    ``Enum``), re-exported from this package so an add-in imports nothing from ``prometheus_client``
    and declares nothing beyond ``kx-mcp-core``. ``**kwargs`` reaches the constructor, so
    ``buckets`` / ``namespace`` / ``unit`` / ``states`` all still work.

    **Why this exists at all.** Constructing a collector registers it on the process-global
    ``REGISTRY`` as a side effect, and Prometheus refuses a duplicate name — so a module imported
    twice (which the test suite does when it reloads to re-resolve settings) raises instead of
    no-opping. This recovers the already-registered collector instead, deriving the registry key
    itself: ``prometheus_client`` exposes no public get-or-create, and the key is not simply ``name``
    (a ``Counter`` munges a trailing ``_total`` away, so ``acme_widgets_enriched_total`` registers
    under ``acme_widgets_enriched`` as well). Making the caller supply that key — the previous shape
    here — meant writing the same metric name twice, in two different spellings, with only one of
    them affecting the output.

    Raises ``ValueError`` if the name is taken by a *different* collector — a different class, or the
    same class with different labels. That is a real collision between two bundles rather than a
    double import, and returning the registered collector anyway would "work" until the first
    ``.labels()`` call failed somewhere unrelated.
    """
    try:
        return cls(name, documentation, labelnames, **kwargs)
    except ValueError as exc:
        existing = _registered(cls, name, exc)
        if existing is None:  # a ValueError for some other reason — a malformed name, say
            raise
        _reject_if_incompatible(existing, cls, name, labelnames, exc)
        return existing


def _base_name(cls: type, name: str) -> str:
    """The bare name a collector registers under, mirroring ``_build_full_name``'s OpenMetrics munge.

    Only ``Counter`` strips a trailing ``_total``; every other type registers its name as given. The
    bare name is always among the registered keys, whatever the type, so one lookup is enough.
    """
    if getattr(cls, "_type", "") == "counter" and name.endswith(_TOTAL_SUFFIX):
        return name[: -len(_TOTAL_SUFFIX)]
    return name


def _registered(cls: type, name: str, exc: ValueError) -> Any:
    """Find the collector already occupying ``name``, or None.

    ``DuplicateTimeseries`` (a ``ValueError`` subclass) carries the colliding names in
    ``.duplicates``, which is the most direct answer; the derived base name is the fallback that needs
    no version floor on ``prometheus-client``. ``_names_to_collectors`` is private, but there is no
    public equivalent and the alternative — a separate unregistered registry — would make the
    collector invisible to the scrape, which defeats the point.
    """
    for key in (*(getattr(exc, "duplicates", None) or ()), _base_name(cls, name), name):
        found = REGISTRY._names_to_collectors.get(key)
        if found is not None:
            return found
    return None


def _reject_if_incompatible(
    existing: Any, cls: type, name: str, labelnames: Sequence[str], exc: ValueError
) -> None:
    """Fail loudly when the registered collector is not the one the caller was asking for.

    Label names are compared via the private ``_labelnames`` because there is no public accessor —
    and deliberately *not* by calling ``.labels(...)`` to probe, which would mint a real child series
    into everyone's scrape.
    """
    found_labels = tuple(getattr(existing, "_labelnames", ()))
    if type(existing) is cls and found_labels == tuple(labelnames):
        return
    raise ValueError(
        f"metric {name!r} is already registered as {type(existing).__name__}{found_labels}, so it "
        f"cannot also be defined as {cls.__name__}{tuple(labelnames)}. Metric names are "
        f"process-global (Prometheus's registry is), so two bundles must not claim the same one — "
        f"prefix yours with your bundle's namespace."
    ) from exc


# One counter and one histogram, labelled by dispatch target. `tool_name` carries whichever target
# the hook saw — a namespaced tool/prompt name, or a resource URI for resource reads.
DISPATCH_COUNTER = metric(
    Counter,
    "kx_mcp_dispatches_total",
    "Total MCP dispatches handled by the container, by target and outcome.",
    ["tool_name", "outcome"],
)

DISPATCH_DURATION = metric(
    Histogram,
    "kx_mcp_dispatch_duration_seconds",
    "Wall-clock duration of an MCP dispatch, by target.",
    ["tool_name"],
)

#: Concurrency, which a counter and a histogram cannot express between them: they say how many
#: dispatches happened and how long each took, never how many were in flight at once. That makes a
#: pile-up invisible — and in this container it is the signal that shows a backend call is holding
#: the event loop, since a blocked loop pins this at 1 while latency climbs.
DISPATCH_IN_PROGRESS = metric(
    Gauge,
    "kx_mcp_dispatches_in_progress",
    "MCP dispatches currently in flight, by target.",
    ["tool_name"],
)


class MetricsMiddleware(Middleware):
    """Record a count + duration for every tool call, resource read, and prompt get.

    Implements the three real FastMCP dispatch hooks — the same three
    :class:`~kx_mcp_core.auth.AuditMiddleware` implements, which is the proof of their names and of
    how the target is read off the message.

    ``outcome`` distinguishes the same three cases the audit line does — ``denied`` for an
    authorization refusal, ``error`` for any other failure, ``ok`` otherwise — classified by
    :mod:`kx_mcp_core.observability._outcome`, which reads both the
    :func:`~kx_mcp_core.auth.authz_decision` (the ``AuthzSlot``) and the result's
    ``isError`` flag. Reading the contextvar requires this middleware to run **inside**
    ``AuditMiddleware``'s scope, since audit resets that slot in its own ``finally``;
    ``make_parent`` guarantees that attach order.

    A failure the tool *returned* is counted only when the tool marked it ``isError: true`` — the
    protocol's own failure flag, which every extension is required to set
    (``kx_mcp_core.results``). Middleware deliberately does not inspect payloads for a backend's
    own error field; see ``_outcome`` for why.

    Duration is recorded for every outcome, including failures — a tool that reliably errors after
    30s is exactly the thing you want in the histogram.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        return await self._observe(target_name(context), context, call_next)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        return await self._observe(target_uri(context), context, call_next)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        return await self._observe(target_name(context), context, call_next)

    async def _observe(self, target: str, context: MiddlewareContext, call_next):
        started = time.perf_counter()
        # Imported here, not at module scope: runtime.py imports `metric` from this module, so a
        # top-level import would be circular. After the first call this is a `sys.modules` hit —
        # the same lazy-import shape `span()` uses on its own hot path.
        from .runtime import ensure_monitor

        # Process-level sampling starts on the first dispatch rather than at construction: this is
        # the first point where a running event loop is guaranteed to exist.
        ensure_monitor()
        self._track(target, +1)
        try:
            try:
                result = await call_next(context)
            except Exception:
                # A capability denial raises AuthorizationDenied; anything else is a real error.
                self._record(target, outcome_for_exception())
                raise
            # A tool can also *return* a failure without raising — a structured denial from the
            # kdb-x data gate, or any error result carrying `isError: true`.
            self._record(target, outcome_for_result(result))
            return result
        finally:
            self._track(target, -1)
            self._observe_duration(target, time.perf_counter() - started)

    @staticmethod
    def _track(target: str, delta: int) -> None:
        """Move the in-flight gauge, swallowing any telemetry failure.

        The decrement runs in ``_observe``'s ``finally`` so it happens on every exit path — a
        success, a raised error, or a returned error result. A gauge that only decrements on the
        happy path drifts upward forever and reads as a permanent pile-up.
        """
        try:
            DISPATCH_IN_PROGRESS.labels(tool_name=target).inc(delta)
        except Exception:  # a broken/exhausted collector must not surface to the caller
            logger.debug("failed to track in-flight dispatch of %s", target, exc_info=True)

    @staticmethod
    def _record(target: str, outcome: str) -> None:
        try:
            DISPATCH_COUNTER.labels(tool_name=target, outcome=outcome).inc()
        except Exception:  # a broken/exhausted collector must not surface to the caller
            logger.debug("failed to count dispatch of %s (%s)", target, outcome, exc_info=True)

    @staticmethod
    def _observe_duration(target: str, elapsed: float) -> None:
        """Record the duration, swallowing any telemetry failure.

        This runs in ``_observe``'s ``finally``, which is the one place a raising collector does real
        damage: an exception here would be *chained* onto whatever the tool was already raising
        (Python sets ``__context__``), so the client would read a confusing "During handling of the
        above exception, another exception occurred" wrapping the real error. Telemetry must never
        displace or decorate a tool's own failure.
        """
        try:
            DISPATCH_DURATION.labels(tool_name=target).observe(elapsed)
        except Exception:  # a broken/exhausted collector must not surface to the caller
            logger.debug("failed to record dispatch duration for %s", target, exc_info=True)


def mount_metrics_route(server, settings, transport: str) -> bool:
    """Mount the Prometheus scrape endpoint on ``server``'s own ASGI app. Returns True if mounted.

    Uses FastMCP 3.x's supported extra-route mechanism, ``@server.custom_route(path, methods)``,
    which appends a Starlette ``Route`` to the app the container already serves — no second app, no
    second port, no FastAPI wrapper.

    The handler returns ``generate_latest(REGISTRY)`` with the Prometheus content type, which is what
    ``prometheus_client.make_asgi_app()`` produces internally. The raw ASGI app itself cannot be used
    here: ``custom_route`` builds a Starlette ``Route``, and Starlette wraps a *function* endpoint via
    ``request_response()`` — so it would call ``prometheus_app(request)`` and fail with a missing
    ``receive``/``send`` (verified against starlette 1.2.1 / fastmcp 3.4.0). Hosting the ASGI app
    would need a ``Mount`` appended to the private ``_additional_http_routes``; ``custom_route`` is
    the public seam, so that is what this uses.

    Skips with a warning when the transport serves no HTTP (``stdio``) — the zero-config default
    posture. Degrade-with-a-warning, never crash.
    """
    if not settings.metrics_enabled:
        return False

    if transport not in HTTP_TRANSPORTS:
        logger.warning(
            "metrics requested (KX_MCP_METRICS=%s) but transport '%s' serves no HTTP endpoint — "
            "the %s scrape route is not mounted; dispatch metrics are still collected in-process. "
            "Use --transport streamable-http (or http) to expose them.",
            settings.metrics,
            transport,
            settings.metrics_path,
        )
        return False

    # Imported here so the module stays importable in the stdio/metrics-off path without pulling in
    # starlette's request/response layer.
    from starlette.requests import Request
    from starlette.responses import Response

    @server.custom_route(settings.metrics_path, methods=["GET"], name="kx_mcp_metrics")
    async def _metrics_endpoint(request: Request) -> Response:  # pragma: no cover - covered via HTTP
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    logger.info("metrics endpoint mounted at %s (Prometheus)", settings.metrics_path)
    return True
