"""Container auth: inbound verification, outbound identity propagation, and capability checks.

Inbound (`KX_MCP_AUTH`) attaches in :func:`kx_mcp_core.make_parent` — a FastMCP ``AuthProvider``
guards every mounted backend, and the audit middleware logs each dispatch. Outbound is the
token-exchange seam a backend tool calls (`exchange`) to mint a backend-shaped credential from the
inbound principal. The `@authorize` decorator adds an optional capability check at the tool
boundary, decided by a pluggable adapter and folded into the same audit line.
"""

from kx_auth_core import (
    AuthzAdapter,
    AuthzDecision,
    AuthzRequest,
    OutboundConfig,
    OutboundCredential,
    authz_adapters,
    decide,
    decode_claims_unverified,
    exchange,
    outbound_strategies,
    register_authz_adapter,
    register_outbound_strategy,
)

from .audit import AuditMiddleware, audit_authentication_denials
from .authorize import (
    AuthorizationDenied,
    AuthzSlot,
    authorize,
    authz_decision,
    begin_authz_dispatch,
    configure_authz,
    end_authz_dispatch,
    require_authz_adapter,
    stamp_authz_decision,
)
from .authz_settings import AuthzSettings
from .principal import current_principal, subject_from
from .providers import auth_modes, build_auth_provider, register_auth_mode
from .settings import AuthSettings

__all__ = [
    # inbound
    "AuthSettings",
    "build_auth_provider",
    "register_auth_mode",
    "auth_modes",
    "current_principal",
    "subject_from",
    "AuditMiddleware",
    "audit_authentication_denials",
    # outbound
    "OutboundConfig",
    "OutboundCredential",
    "exchange",
    "register_outbound_strategy",
    "outbound_strategies",
    "decode_claims_unverified",
    # capability-check decision seam
    "AuthzRequest",
    "AuthzDecision",
    "AuthzAdapter",
    "decide",
    "register_authz_adapter",
    "authz_adapters",
    # capability-check fastmcp binding: the @authorize decorator + its config
    "authorize",
    "AuthzSettings",
    "AuthorizationDenied",
    "configure_authz",
    "require_authz_adapter",
    "AuthzSlot",
    "authz_decision",
    "begin_authz_dispatch",
    "end_authz_dispatch",
    "stamp_authz_decision",
]
