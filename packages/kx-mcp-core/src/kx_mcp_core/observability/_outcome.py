"""Classify a dispatch's outcome — shared by the metrics + tracing middleware and the audit line.

One function so the counter's ``outcome`` label, the span's ``mcp.outcome`` attribute, and the audit
line can never disagree about the same dispatch. The three values:

``denied``
    An authorization refusal. Recognised from the :func:`authz_decision` (the ``AuthzSlot``), which
    the refusing layer stamps — the ``@authorize`` decorator (PEP-1), the explicit data-gate consult,
    and the q-side ``'denied`` translation (PEP-2). A denial takes precedence over ``error``: it
    reaches middleware either as a raised ``AuthorizationDenied`` *or* as a returned
    ``permission_denied`` result, and both must land in the same series.

``error``
    Any other failure: an exception escaping the dispatch, or a result the tool marked
    ``isError: true``.

``ok``
    Everything else.

**Why ``is_error`` and not the payload.** Middleware must not know a backend's payload conventions —
``{"status": "error"}`` is a kdbx/kdbai spelling, not a protocol fact, and a third-party bundle is
free to use its own. ``isError`` is the protocol's own failure flag, so reading it keeps this module
backend-agnostic while still catching a failure the tool *returned* rather than raised. Bundles are
required to set it (``kx_mcp_core.results``, ``docs/extending.md`` § Signalling failure); a bundle
that does not comply is under-reported here rather than misreported anywhere else.
"""

from __future__ import annotations

from typing import Any, Optional

from ..auth.authorize import authz_decision as _authz_decision

OUTCOME_OK = "ok"
OUTCOME_DENIED = "denied"
OUTCOME_ERROR = "error"


def _is_denied() -> bool:
    """True when the dispatch carried an authorization check that refused."""
    decision = _authz_decision()
    return decision is not None and not decision.allowed


def outcome_for_exception() -> str:
    """The outcome for a dispatch that raised: ``denied`` if authz refused, else ``error``."""
    return OUTCOME_DENIED if _is_denied() else OUTCOME_ERROR


def outcome_for_result(result: Optional[Any]) -> str:
    """The outcome for a dispatch that returned normally.

    ``denied`` when an authorization layer refused (the tool returns a structured
    ``permission_denied`` rather than raising), ``error`` when the tool marked the result
    ``isError: true``, else ``ok``. ``getattr`` so a hook whose result is not a ``ToolResult`` —
    a resource read, a prompt get — degrades to ``ok`` instead of raising inside middleware.
    """
    if _is_denied():
        return OUTCOME_DENIED
    if getattr(result, "is_error", False):
        return OUTCOME_ERROR
    return OUTCOME_OK


def authz_decision() -> Optional[Any]:
    """The dispatch's authz decision, if any — for the span attributes that record it."""
    return _authz_decision()
