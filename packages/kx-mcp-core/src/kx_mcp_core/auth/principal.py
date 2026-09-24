"""Access the validated inbound principal from within a request.

After the parent's inbound auth validates a bearer, FastMCP exposes the resulting ``AccessToken``
through a contextvar reachable anywhere in the request — *including inside a mounted backend's
tools* (verified: the parent-auth principal crosses the mount boundary). Extensions should read it
directly via ``from fastmcp.server.dependencies import get_access_token``; this thin wrapper is the
container's own convenience (used by the audit middleware) and returns ``None`` instead of raising
when there is no authenticated principal (the ``KX_MCP_AUTH=unset`` posture).
"""

from __future__ import annotations

from typing import Optional

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token


def current_principal() -> Optional[AccessToken]:
    """The validated ``AccessToken`` for the in-flight request, or ``None`` when unauthenticated."""
    try:
        return get_access_token()
    except Exception:
        # No auth configured / no token on this request — anonymous, not an error.
        return None


def subject_from(token: Optional[AccessToken]) -> str:
    """The audited/authorized identity of ``token``: ``sub`` claim → client id → ``"anonymous"``.

    One derivation, deliberately shared. The ``@authorize`` capability check and the audit
    middleware must name the *same* subject: for any user token ``sub`` is the human and
    ``client_id``/``azp`` is the app, so two independent derivations meant the decision was made
    about one identity while the audit line recorded another — leaving the record unable to answer
    "who was granted this?". Import this rather than re-spelling the precedence.
    """
    claims = getattr(token, "claims", None) or {}
    return claims.get("sub") or (getattr(token, "client_id", None) if token else None) or "anonymous"
