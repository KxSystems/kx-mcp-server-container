"""Map a q-side identity-assertion denial into a clean structured tool response.

The q `kx.auth` module (and permission-check functions built on it) signal a refusal via
`'denied: ...`, which PyKX surfaces as a `QError` whose text starts with "denied". Tools translate
that into a structured `permission_denied` envelope so the agent receives a clear, actionable denial
rather than a raw stack trace — the graceful-degradation invariant (never crash the container; turn
an authorization refusal into a clean signal). Shared so every kdb-x tool surfaces denials
identically, the same way the connection-layer bind covers every tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # typing only — keep this module import-light
    from kx_auth_core.authz import AuthzDecision


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
