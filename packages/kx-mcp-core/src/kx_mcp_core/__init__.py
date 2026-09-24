"""kx-mcp-core — the assembly layer for the FastMCP-composition container.

It provides the assembly seam (`make_parent`, `mount_bundle`, `load_build_server`), a thin launcher,
inbound auth (the `KX_MCP_AUTH` verifier seam `build_auth_provider` + the audit middleware, both
attached in `make_parent`), the outbound token-exchange seam (`exchange` +
`register_outbound_strategy`, called from a backend tool), the tool-result contract (`tool_result` /
`error_result` — a failed dispatch must carry `isError: true`), and the opt-in observability seams
(`KX_MCP_METRICS` / `KX_MCP_TRACING` — middleware attached in `make_parent`, the scrape route mounted
by `mount_metrics_route`).
"""

from .assembly import load_build_server, make_parent, mount_bundle, try_mount_bundle
from .discovery import register_components
from .logging import configure_logging
from .results import (
    FAILURE_STATUSES,
    STATUS_DENIED,
    STATUS_ERROR,
    STATUS_OK,
    error_result,
    tool_result,
)
from .observability import (
    MetricsMiddleware,
    ObservabilitySettings,
    TracingMiddleware,
    init_tracing,
    metric,
    mount_metrics_route,
    span,
)
from .auth import (
    AuditMiddleware,
    AuthSettings,
    OutboundConfig,
    OutboundCredential,
    auth_modes,
    build_auth_provider,
    current_principal,
    exchange,
    outbound_strategies,
    register_auth_mode,
    register_outbound_strategy,
)

__all__ = [
    "make_parent",
    "mount_bundle",
    "try_mount_bundle",
    "load_build_server",
    "register_components",
    "configure_logging",
    # inbound
    "build_auth_provider",
    "register_auth_mode",
    "auth_modes",
    "AuthSettings",
    "AuditMiddleware",
    "current_principal",
    # outbound
    "exchange",
    "register_outbound_strategy",
    "outbound_strategies",
    "OutboundConfig",
    "OutboundCredential",
    # observability (opt-in: KX_MCP_METRICS / KX_MCP_TRACING)
    "ObservabilitySettings",
    "MetricsMiddleware",
    "TracingMiddleware",
    "mount_metrics_route",
    "init_tracing",
    # the bundle-author instrumentation surface (the collector classes live on
    # kx_mcp_core.observability, next to `metric`)
    "metric",
    "span",
    # tool results (required: a failed dispatch must carry isError: true)
    "tool_result",
    "error_result",
    "STATUS_OK",
    "STATUS_ERROR",
    "STATUS_DENIED",
    "FAILURE_STATUSES",
]
