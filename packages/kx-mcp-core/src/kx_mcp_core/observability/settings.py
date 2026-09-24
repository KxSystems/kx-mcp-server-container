"""Container observability configuration (the ``KX_MCP_METRICS`` / ``KX_MCP_TRACING`` families).

Two independent seams live in one settings model, each selected by its own **bare** env var —
``KX_MCP_METRICS`` and ``KX_MCP_TRACING`` — with the mode-specific detail under the matching
``KX_MCP_METRICS_*`` / ``KX_MCP_TRACING_*`` prefix. That is the same convention the inbound
(``KX_MCP_AUTH``) and authorization (``KX_MCP_AUTHZ``) seams use: the selector is the bare variable,
never ``..._MODE``.

Mirrors :class:`kx_mcp_core.auth.AuthzSettings` in shape so the inbound, authz, and observability
seams read alike — empty/whitespace collapses to off, and the model is constructible by name in
tests/glue (``ObservabilitySettings(metrics="prometheus")``).

Because this one model spans two env families, fields carry explicit ``validation_alias`` names
rather than sharing a single ``env_prefix``.

Both seams default **off**: an unconfigured container behaves exactly as it does today.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The modes each selector accepts. "" (off) is always valid and is the default.
METRICS_MODES = ("prometheus",)
TRACING_MODES = ("otlp",)


class ObservabilitySettings(BaseSettings):
    """Resolved observability config. Read from the environment by default."""

    model_config = SettingsConfigDict(
        # No env_prefix: this model spans two families (KX_MCP_METRICS*, KX_MCP_TRACING*), so each
        # field names its own env var explicitly via validation_alias.
        populate_by_name=True,
        extra="ignore",
    )

    # --- metrics (Prometheus) -------------------------------------------------------------------
    # The selector is the bare KX_MCP_METRICS var (not KX_MCP_METRICS_MODE) — like KX_MCP_AUTH(Z).
    # "" = off (no middleware, no /metrics route). "prometheus" = counters + the scrape endpoint.
    metrics: str = Field(default="", validation_alias="KX_MCP_METRICS")

    # Where the scrape endpoint is mounted on the container's own ASGI app. Only consulted under
    # an HTTP transport — stdio has no listener to mount it on (see mount_metrics_route).
    metrics_path: str = Field(default="/metrics", validation_alias="KX_MCP_METRICS_PATH")

    # --- tracing (OpenTelemetry) ----------------------------------------------------------------
    # "" = off (nothing imported, no spans). "otlp" = export spans over OTLP.
    tracing: str = Field(default="", validation_alias="KX_MCP_TRACING")

    # The collector spans are pushed to. Unset falls back to the OTel SDK's own default endpoint
    # (localhost:4317 for the gRPC exporter), so this is optional rather than required.
    otlp_endpoint: Optional[str] = Field(
        default=None, validation_alias="KX_MCP_TRACING_OTLP_ENDPOINT"
    )

    # Stamped on every span as the `service.name` resource attribute.
    service_name: str = Field(default="kx-mcp", validation_alias="KX_MCP_TRACING_SERVICE_NAME")

    # --- normalisation -------------------------------------------------------------------------

    @field_validator("metrics", "tracing", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> str:
        """Empty / whitespace / unset all collapse to ``""`` (the seam is off)."""
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return ""

    @field_validator("metrics")
    @classmethod
    def _known_metrics_mode(cls, value: str) -> str:
        return cls._check_mode(value, METRICS_MODES, "KX_MCP_METRICS")

    @field_validator("tracing")
    @classmethod
    def _known_tracing_mode(cls, value: str) -> str:
        return cls._check_mode(value, TRACING_MODES, "KX_MCP_TRACING")

    @staticmethod
    def _check_mode(value: str, known: tuple, var: str) -> str:
        """Reject an unrecognised mode loudly at startup.

        A typo (``KX_MCP_METRICS=prometheous``) would otherwise silently leave the seam **off** —
        the operator would see no metrics and no reason why. Failing construction surfaces it as a
        clear config error at startup, matching how ``build_auth_provider`` rejects an unknown
        ``KX_MCP_AUTH`` mode rather than quietly disabling auth.
        """
        if value and value not in known:
            raise ValueError(
                f"unknown {var} mode {value!r}; known modes: {list(known)} (or empty for off)"
            )
        return value

    @field_validator("metrics_path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        """Ensure a leading slash and a sane default — Starlette asserts routed paths start with '/'."""
        value = (value or "").strip()
        if not value:
            return "/metrics"
        return value if value.startswith("/") else f"/{value}"

    # --- convenience ---------------------------------------------------------------------------

    @property
    def metrics_enabled(self) -> bool:
        """True when the metrics seam is on (``KX_MCP_METRICS=prometheus``)."""
        return self.metrics == "prometheus"

    @property
    def tracing_enabled(self) -> bool:
        """True when the tracing seam is on (``KX_MCP_TRACING=otlp``)."""
        return self.tracing == "otlp"

    @property
    def enabled(self) -> bool:
        """True when *either* seam is on — the cheap check before doing any observability work."""
        return self.metrics_enabled or self.tracing_enabled
