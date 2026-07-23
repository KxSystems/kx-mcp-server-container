"""kx-mcp-core — the assembly layer for the FastMCP-composition container.

It provides the assembly seam (`make_parent`, `mount_bundle`, `load_build_server`), a thin launcher,
inbound auth (the `KX_MCP_AUTH` verifier seam `build_auth_provider` + the audit middleware, both
attached in `make_parent`), and the outbound token-exchange seam (`exchange` +
`register_outbound_strategy`, called from a backend tool).
"""

from .assembly import load_build_server, make_parent, mount_bundle, try_mount_bundle
from .discovery import register_components
from .logging import configure_logging
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
]
