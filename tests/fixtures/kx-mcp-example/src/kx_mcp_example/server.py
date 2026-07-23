"""The example bundle's server — same contract as the kdbx bundle: build_server() -> FastMCP."""

from __future__ import annotations

import json as _json

from fastmcp import FastMCP

from kx_auth_core import OutboundConfig, decode_claims_unverified, exchange
from kx_mcp_core.auth import authorize, current_principal


def build_server() -> FastMCP:
    """Build a minimal FastMCP server with three demo tools (the extension seam)."""
    mcp = FastMCP("example")

    @mcp.tool()
    def echo(text: str) -> str:
        """Echo the input text back."""
        return text

    @mcp.tool()
    def whoami() -> str:
        """Return the authenticated principal's client_id, or 'anonymous' when auth is unset."""
        principal = current_principal()
        return "anonymous" if principal is None else principal.client_id

    @mcp.tool()
    @authorize(action="write", resource="example:thing")
    async def privileged() -> str:
        """A capability-gated demo tool: requires a group grant for ``example:write``.

        Used by the authz integration test to prove allow/deny by principal × action over the wire
        (route-only when KX_MCP_AUTHZ is unset). Decorated here, not on a real backend tool —
        today's shipped tools have no capability concern distinct from their data gate.
        """
        return "did the privileged thing"

    @mcp.tool()
    async def propagate(
        strategy: str,
        token_url: str = "",
        audience: str = "",
        client_id: str = "",
        client_secret: str = "",
    ) -> str:
        """Exercise the inbound→outbound wiring under composition.

        Reads current_principal() (proves the bearer crossed the mount boundary), then calls
        exchange() with the given OutboundConfig. Returns a JSON envelope with:
          principal_sub: the inbound principal's client_id (null if anonymous)
          exchanged_token_aud: the audience claim from the exchanged token
          strategy: the strategy that produced the credential
        Used by test_outbound_integration.py to assert both sides in one tool call.
        """
        principal = current_principal()
        subject_token = principal.token if principal is not None else None
        config = OutboundConfig(
            strategy=strategy,
            token_url=token_url or None,
            audience=audience or None,
            client_id=client_id or None,
            client_secret=client_secret or None,
        )
        cred = await exchange(config, subject_token)
        aud = decode_claims_unverified(cred.access_token).get("aud")
        return _json.dumps({
            "principal_sub": principal.client_id if principal is not None else None,
            "exchanged_token_aud": aud,
            "strategy": cred.strategy,
        })

    return mcp
