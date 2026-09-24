"""Lean, fastmcp-free shared core for kx auth.

Holds the shared, fastmcp-free auth mechanisms imported by the container (``kx-mcp-core``), the
backend bundles, and the client-side ``kx auth`` CLI alike:
- **inbound** — the ``KX_MCP_AUTH`` config contract (:class:`AuthSettings`) + a joserfc-based
  :func:`verify_token` (the one implementation of bearer validation; the container also keeps
  FastMCP's ``JWTVerifier`` for serving);
- **outbound** — the token-exchange seam (:func:`exchange` + :func:`register_outbound_strategy`),
  so identity propagation is one implementation reused by every backend and the CLI.

Deliberately depends on no fastmcp, so bundles and the CLI can use it without pulling in the server.
"""

from .assertion import project_from_claims, project_principal
from .authz import (
    ROUTE_ONLY_MODES,
    AuthzAdapter,
    AuthzDecision,
    AuthzRequest,
    authz_adapters,
    decide,
    register_authz_adapter,
)
from .outbound import (
    OutboundConfig,
    OutboundCredential,
    decode_claims_unverified,
    exchange,
    outbound_strategies,
    register_outbound_strategy,
)
from .settings import AuthSettings
from .verify import (
    AUTH_REQUIRED,
    DENIED,
    ERROR,
    OK,
    VerifyResult,
    resolve_public_key,
    verify_token,
)

__all__ = [
    # inbound
    "AuthSettings",
    "verify_token",
    "VerifyResult",
    "resolve_public_key",
    "OK",
    "ERROR",
    "AUTH_REQUIRED",
    "DENIED",
    # outbound
    "exchange",
    "register_outbound_strategy",
    "outbound_strategies",
    "OutboundConfig",
    "OutboundCredential",
    "decode_claims_unverified",
    # identity assertion (plain kdb+)
    "project_principal",
    "project_from_claims",
    # authorization decision seam
    "AuthzRequest",
    "AuthzDecision",
    "AuthzAdapter",
    "decide",
    "register_authz_adapter",
    "authz_adapters",
    "ROUTE_ONLY_MODES",
]
