"""Container authorization configuration (the ``KX_MCP_AUTHZ`` family) — capability-check gating.

The *strategy* lives in the bare ``KX_MCP_AUTHZ`` env var (``""`` = off / route-only — the default,
matching the inbound ``KX_MCP_AUTH=unset`` opt-in default; ``static`` = the built-in YAML
capability-grant adapter). The strategy-specific detail lives under the ``KX_MCP_AUTHZ_*`` prefix.
This is the container's authz config fragment — distinct from the inbound ``KX_MCP_AUTH*`` family
and from each extension's own prefix.

Mirrors :class:`kx_mcp_core.auth.AuthSettings` in shape so the inbound, outbound, and authz seams
read alike.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthzSettings(BaseSettings):
    """Resolved authorization config. Read from the environment by default; constructible by name in
    tests/glue (``AuthzSettings(mode="static", policy_file=...)``)."""

    model_config = SettingsConfigDict(
        env_prefix="KX_MCP_AUTHZ_",
        populate_by_name=True,
        extra="ignore",
    )

    # The strategy selector is the bare KX_MCP_AUTHZ var (not KX_MCP_AUTHZ_MODE) — like KX_MCP_AUTH.
    # "" = no adapter = route-only (the decorator allows; the backend's data gate is the sole
    # authority). "static" = the built-in YAML capability adapter (capability.py).
    mode: str = Field(default="", validation_alias="KX_MCP_AUTHZ")

    # static: path to the YAML capability-grant store (shape documented in docs/auth.md).
    policy_file: Optional[str] = None

    # Which inbound claim carries the subject's groups the capability grant intersects. The
    # Python side only sees raw claims, so the path is configurable — for Keycloak typically
    # "groups"; for Entra "roles" or a groups mapping (GUIDs).
    groups_claim: str = "groups"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> str:
        """Empty / whitespace / unset all collapse to ``""`` (route-only — authz off)."""
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return ""
