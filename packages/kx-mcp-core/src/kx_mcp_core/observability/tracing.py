"""OpenTelemetry tracing: one span per tool / resource / prompt dispatch.

Rides the same parent ``add_middleware`` seam as :class:`~kx_mcp_core.auth.AuditMiddleware` and
:class:`~kx_mcp_core.observability.MetricsMiddleware`, implementing the same three real dispatch
hooks — so every mounted backend is traced with no per-backend wiring, and the span's target is
already namespaced (``kdbx_run_sql_query``).

**Everything OpenTelemetry is imported lazily.** ``opentelemetry`` is only touched inside
:func:`init_tracing` and on the middleware's first span, so a container running with
``KX_MCP_TRACING`` unset never pays that import — the same deferral
:func:`kx_mcp_core.auth.configure_authz` uses for ``yaml`` + the static policy adapter, which loads
only under ``KX_MCP_AUTHZ=static``.

That discipline is also what lets the OpenTelemetry SDK + OTLP exporter ship as the optional
``kx-mcp-core[tracing]`` extra rather than a hard dependency: with them absent this module still
imports and the container still serves, and :func:`init_tracing` reports the missing extra by name.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any, Optional
from urllib.parse import urlsplit

from fastmcp.server.middleware import Middleware, MiddlewareContext

from ._outcome import (
    OUTCOME_DENIED,
    authz_decision,
    outcome_for_exception,
    outcome_for_result,
)
from ._target import target_name, target_uri

logger = logging.getLogger(__name__)

# Span attribute keys. `mcp.target` is the namespaced tool/prompt name or the resource URI.
ATTR_ACTION = "mcp.action"
ATTR_TARGET = "mcp.target"
ATTR_OUTCOME = "mcp.outcome"
ATTR_AUTHZ_DECISION = "mcp.authz.decision"
ATTR_AUTHZ_ADAPTER = "mcp.authz.adapter"

# How long the startup reachability probe waits for a TCP connect before calling the collector unreachable.
_PROBE_TIMEOUT_SECONDS = 2.0
_DEFAULT_OTLP_HOSTPORT = ("localhost", 4317)  # the gRPC exporter's own default

# Set once by init_tracing so a second call is a no-op (the launcher and hand-written glue may both
# reach it, and installing two TracerProviders would drop spans on the floor).
_initialised = False


def _probe_target(endpoint: Optional[str]) -> tuple[str, int, str]:
    """Resolve ``(host, port, display)`` for the collector the exporter will contact.

    Accepts the http(s) and bare ``host:port`` (gRPC) forms; a missing port takes the scheme's default
    (80 / 443) or the OTLP gRPC port 4317 for a bare host. ``display`` is ``scheme://host:port`` only,
    so userinfo, path and query (where credentials could sit) never reach a log line.
    """
    endpoint = (
        endpoint
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    if not endpoint:
        host, port = _DEFAULT_OTLP_HOSTPORT
        return host, port, f"{host}:{port}"
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    host = parts.hostname or _DEFAULT_OTLP_HOSTPORT[0]
    scheme = parts.scheme.lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme, _DEFAULT_OTLP_HOSTPORT[1])
    shown_host = f"[{host}]" if ":" in host else host
    return host, port, f"{scheme + '://' if scheme else ''}{shown_host}:{port}"


def _probe_collector(endpoint: Optional[str]) -> None:
    """Best-effort TCP connect to the collector; warn if it cannot be reached. Runs off-thread.

    Exporter construction never contacts the endpoint, so this is the only startup-time signal that
    spans have nowhere to go. It proves the port accepts connections, not that OTLP is spoken there.
    Never raises: it must not disturb a container that is otherwise serving.
    """
    try:
        host, port, shown = _probe_target(endpoint)
        try:
            with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
                return
        except OSError as exc:
            reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "OTLP endpoint %s is unreachable (%s); serving without traces until it becomes reachable",
            shown,
            reason,
        )
    except Exception as exc:  # pragma: no cover - a probe bug must never surface
        logger.debug("OTLP reachability probe failed to run: %s", exc)


def _start_reachability_probe(endpoint: Optional[str]) -> None:
    """Run :func:`_probe_collector` on a daemon thread so startup is never delayed or held open."""
    threading.Thread(
        target=_probe_collector, args=(endpoint,), name="kx-mcp-otlp-probe", daemon=True
    ).start()


def init_tracing(settings) -> bool:
    """Install a ``TracerProvider`` exporting spans over OTLP. Returns True if tracing was set up.

    No-op (returns False) when ``KX_MCP_TRACING`` is off, and idempotent when called more than once.
    Every ``opentelemetry`` import happens *inside* this function — see the module docstring.

    Constructing the exporter never contacts the collector, so an unreachable endpoint is not a setup
    failure; a background TCP probe (:func:`_probe_collector`) warns about it instead, and the
    exporter's own warnings follow once spans are dropped.

    A failure to construct the exporter (a bad config, a missing optional dependency) is
    logged and swallowed: telemetry is not worth taking the container down for, which is the same
    posture ``try_mount_bundle`` takes for a backend that won't come up.
    """
    global _initialised

    if not settings.tracing_enabled:
        return False
    if _initialised:
        logger.debug("tracing already initialised — skipping re-init")
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": settings.service_name})
        provider = TracerProvider(resource=resource)
        # Endpoint None -> the SDK falls back to its own default (localhost:4317) / OTEL_* env.
        exporter = (
            OTLPSpanExporter(endpoint=settings.otlp_endpoint)
            if settings.otlp_endpoint
            else OTLPSpanExporter()
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except ImportError as exc:
        # The `tracing` extra is not installed. Caught before the general handler below (ImportError
        # *is* an Exception) purely so the operator reads the fix instead of a bare
        # ModuleNotFoundError: opentelemetry-api ships with fastmcp, but the SDK and OTLP exporter
        # this seam needs live only in the extra. Same non-fatal outcome as any other setup failure.
        logger.warning(
            "tracing requested (KX_MCP_TRACING=%s) but the OpenTelemetry tracing extra is not "
            "installed (%s: %s); serving without traces. Install it with: "
            "pip install 'kx-mcp-core[tracing]'",
            settings.tracing,
            type(exc).__name__,
            exc,
        )
        return False
    except Exception as exc:  # telemetry must never take the container down
        logger.warning(
            "tracing requested (KX_MCP_TRACING=%s) but setup failed (%s: %s); serving without traces",
            settings.tracing,
            type(exc).__name__,
            exc,
        )
        return False

    _initialised = True
    _start_reachability_probe(settings.otlp_endpoint)
    logger.info(
        "tracing enabled: exporter=otlp endpoint=%s service.name=%s",
        settings.otlp_endpoint or "(SDK default)",
        settings.service_name,
    )
    return True


def reset_tracing_for_tests() -> None:
    """Clear the one-shot init guard. Test-only seam; not part of the public surface."""
    global _initialised
    _initialised = False


class TracingMiddleware(Middleware):
    """Open one span per dispatch, recording the outcome and any exception.

    Mirrors :class:`~kx_mcp_core.auth.AuditMiddleware`'s try/except/finally shape: the exception is
    recorded on the span and re-raised (never swallowed), and the span is always closed in the
    ``finally`` — here by the ``start_as_current_span`` context manager, which ends the span on any
    exit path.

    ``outcome`` uses the same three-way split as the audit line and the metrics counter, classified
    by the shared :mod:`kx_mcp_core.observability._outcome` helper — which reads the
    :func:`~kx_mcp_core.auth.authz_decision` (the ``AuthzSlot``) (so this middleware must
    run **inside** ``AuditMiddleware``'s scope, since audit resets that slot in its own ``finally``;
    ``make_parent`` guarantees that attach order) *and* the result's ``isError`` flag, so a failure
    the tool returned rather than raised is not spanned as a success.
    """

    def __init__(self, tracer: Optional[Any] = None) -> None:
        # Injectable for tests; otherwise resolved lazily on first span so constructing the
        # middleware never imports opentelemetry.
        self._tracer = tracer

    def _get_tracer(self):
        if self._tracer is None:
            from opentelemetry import trace

            self._tracer = trace.get_tracer("kx_mcp_core.observability")
        return self._tracer

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        return await self._trace("tool_invoke", target_name(context), context, call_next)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        return await self._trace("resource_read", target_uri(context), context, call_next)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        return await self._trace("prompt_get", target_name(context), context, call_next)

    async def _trace(self, action: str, target: str, context: MiddlewareContext, call_next):
        from opentelemetry.trace import Status, StatusCode

        tracer = self._get_tracer()
        # record_exception/set_status_on_exception off: the SDK would ALSO record the exception as it
        # escapes the `with`, giving every failed dispatch two identical `exception` events. We record
        # it once ourselves below, alongside the outcome + authz attributes. The context manager still
        # ends the span on every exit path — that is the `finally` guarantee.
        with tracer.start_as_current_span(
            f"mcp.{action}", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute(ATTR_ACTION, action)
            span.set_attribute(ATTR_TARGET, target)
            try:
                result = await call_next(context)
            except Exception as exc:
                _stamp_decision(span, authz_decision())
                span.set_attribute(ATTR_OUTCOME, outcome_for_exception())
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            outcome = outcome_for_result(result)
            _stamp_decision(span, authz_decision())
            span.set_attribute(ATTR_OUTCOME, outcome)
            if outcome != "ok":
                # A failure the tool *returned* rather than raised — a structured denial, or an
                # error result carrying `isError: true`. Not an exception span, but it must not read
                # as a clean success either.
                span.set_status(
                    Status(
                        StatusCode.ERROR,
                        "authorization denied" if outcome == OUTCOME_DENIED else "tool error",
                    )
                )
            return result


def _stamp_decision(span, decision) -> None:
    """Fold the capability-check decision onto the span, so a trace answers who/what/decision."""
    if decision is None:
        return
    span.set_attribute(ATTR_AUTHZ_DECISION, "allow" if decision.allowed else "deny")
    if decision.adapter:
        span.set_attribute(ATTR_AUTHZ_ADAPTER, decision.adapter)
