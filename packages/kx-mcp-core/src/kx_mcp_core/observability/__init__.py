"""Container observability: Prometheus metrics and OpenTelemetry tracing, both opt-in.

Both seams attach in :func:`kx_mcp_core.make_parent` as parent middleware — the same seam inbound
auth and the audit line use — so one attach point observes every mounted backend, and the dispatch
target is already namespaced (``kdbx_run_sql_query``) by the time middleware sees it. There is no
second server and no second port: the Prometheus scrape route is a plain HTTP route on the container's
own ASGI app (:func:`mount_metrics_route`), which exists only under an HTTP transport.

Selected by the bare ``KX_MCP_METRICS`` / ``KX_MCP_TRACING`` env vars (the ``KX_MCP_AUTH`` /
``KX_MCP_AUTHZ`` convention); both default off, so an unconfigured container is unchanged.

**For bundle authors** this package is also the whole instrumentation surface — :func:`metric` with
the collector classes below, and :func:`span` — so an add-in imports neither ``prometheus_client`` nor
``opentelemetry`` and declares nothing beyond ``kx-mcp-core``. See ``docs/extending.md``
§ Observability for bundle authors.
"""

# The Prometheus collector classes, re-exported unchanged so a bundle's dependency list stays
# `kx-mcp-core` alone. A plain alias, not a wrapper: the full API stays available (including `.time()`
# and the constructor kwargs), and which metrics library the container uses stays the container's
# choice to change — it has already changed once (OpenTelemetry's Metrics API, then back). NB `Enum`
# here is Prometheus's state-set metric, not `enum.Enum`.
from prometheus_client import Counter, Enum, Gauge, Histogram, Info, Summary

from .instrument import TRACER_NAME, span
from .metrics import (
    DISPATCH_COUNTER,
    DISPATCH_DURATION,
    DISPATCH_IN_PROGRESS,
    HTTP_TRANSPORTS,
    MetricsMiddleware,
    metric,
    mount_metrics_route,
)
from .runtime import (
    BUILD_INFO,
    EVENT_LOOP_LAG,
    SAMPLE_INTERVAL_SECONDS,
    THREADS,
    ensure_monitor,
    set_build_info,
)
from .settings import METRICS_MODES, TRACING_MODES, ObservabilitySettings
from .tracing import TracingMiddleware, init_tracing

__all__ = [
    # config
    "ObservabilitySettings",
    "METRICS_MODES",
    "TRACING_MODES",
    # metrics
    "MetricsMiddleware",
    "mount_metrics_route",
    "DISPATCH_COUNTER",
    "DISPATCH_DURATION",
    "DISPATCH_IN_PROGRESS",
    "HTTP_TRANSPORTS",
    # process/runtime metrics
    "BUILD_INFO",
    "EVENT_LOOP_LAG",
    "THREADS",
    "SAMPLE_INTERVAL_SECONDS",
    "ensure_monitor",
    "set_build_info",
    # tracing
    "TracingMiddleware",
    "init_tracing",
    # the bundle-author instrumentation surface
    "metric",
    "span",
    "TRACER_NAME",
    "Counter",
    "Enum",
    "Gauge",
    "Histogram",
    "Info",
    "Summary",
]
