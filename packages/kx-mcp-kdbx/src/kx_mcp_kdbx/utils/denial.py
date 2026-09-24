"""Map a q-side identity-assertion denial into a clean structured tool response.

The q `kx.auth` module (and permission-check functions built on it) signal a refusal via
`'denied: ...`, which PyKX surfaces as a `QError` whose text starts with "denied". Tools translate
that into a structured `permission_denied` envelope so the agent receives a clear, actionable denial
rather than a raw stack trace — the graceful-degradation invariant (never crash the container; turn
an authorization refusal into a clean signal). Shared so every kdb-x tool surfaces denials
identically, the same way the connection-layer bind covers every tool.

The envelopes here are *payloads*, not results: the caller wraps them so the dispatch carries
`isError: true` (`kx_mcp_core.tool_result`, required — see `docs/extending.md` § Signalling
failure), and `record_denial` stamps the authz decision so the refusal is counted `denied` rather
than `error`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # typing only — keep this module import-light
    from kx_auth_core.authz import AuthzDecision


def record_denial(reason: str, *, adapter: str = "kx_auth_qside") -> None:
    """Stamp a deny decision with ``stamp_authz_decision`` so audit + metrics see the refusal.

    A denial the tool *returns* (rather than raises) is invisible to the parent middleware unless
    something records it: ``isError: true`` alone makes it an ``error``, which is honest but loses
    the fact that it was an **authorization** outcome. The explicit data-gate consult
    (``consult_data_gate``) already does this; this is the same stamp for the q-side ``'denied``
    translation below, so all three refusal paths — the ``@authorize`` capability check, the
    explicit consult, and a q-side ``require``/``authorize`` refusal surfaced as a ``QError`` —
    land in ``outcome="denied"`` instead of two of them landing there and one reading as a success.

    Best-effort by design: wrapped in ``try``/``ImportError`` for the standalone-bundle posture
    (no container, so no audit middleware and no contextvar to stamp), exactly as
    ``consult_data_gate`` does.
    """
    try:
        from kx_auth_core.authz import AuthzDecision
        from kx_mcp_core.auth import stamp_authz_decision

        stamp_authz_decision(AuthzDecision(allowed=False, adapter=adapter, reason=reason))
    except ImportError:  # standalone bundle posture — no container, no audit middleware
        logger.debug("no container present; q-side denial not recorded for audit")


def is_denial(exc: Exception) -> bool:
    """True iff `exc` is a q-side authorization refusal (the `'denied: ...` convention).

    The single source for the convention `.kx.auth.require` / `.kx.auth.authorize` (and any
    permission-check function built on them) raise. Both the q-side data gate (the `.s.e` wrapper)
    and the container-side capability check (`authz_kx_rbac.rbac_check`) signal denial this way, so
    they share this predicate rather than re-spelling the prefix test.
    """
    return str(exc).strip().lower().startswith("denied")


def denial_response(exc: Exception) -> Optional[dict[str, Any]]:
    """Return a structured `permission_denied` dict for a q-side denial, else None.

    Matches the `denied: ...` convention `.kx.auth.require` (and permission-check functions) raise.
    Returns None for any other error so the caller's existing handling is unchanged.
    """
    if is_denial(exc):
        msg = str(exc).strip()
        # Record it as an authorization outcome before returning, so the dispatch is counted
        # `denied` rather than `error` — see record_denial.
        record_denial(msg)
        return {
            "status": "error",
            "error_type": "permission_denied",
            "message": f"Access denied by the data layer: {msg}",
            "technical_details": msg,
        }
    return None


def denial_from_decision(decision: "AuthzDecision") -> dict[str, Any]:
    """The `permission_denied` envelope for a container-side data-gate ``AuthzDecision``.

    Same shape as :func:`denial_response` (one denial vocabulary for the agent, whichever layer
    refused), for the explicit-consult path where the verdict arrives as a decision object rather
    than a q exception. Two shapes: a hard deny (``allowed=False``), and an allow-with-obligations
    the caller surfaces as a denial-with-guidance — the SQL tool cannot rewrite a query down to the
    entitled subset, so it returns the ``entitled``/``denied`` lists and the agent re-scopes its
    query instead of dead-ending.
    """
    msg = (decision.reason or "access denied by the data-entitlement gate").strip()
    envelope: dict[str, Any] = {
        "status": "error",
        "error_type": "permission_denied",
        "message": f"Access denied by the data layer: {msg}",
        "technical_details": msg,
    }
    obligations = decision.obligations or {}
    denied = list(obligations.get("denied", []))
    entitled = list(obligations.get("entitled", []))
    if denied:
        envelope["denied_tables"] = denied
    if entitled:
        envelope["entitled_tables"] = entitled
        envelope["message"] += (
            f". You are entitled to: {', '.join(entitled)}"
            " — re-issue the query using only those tables."
        )
    return envelope
