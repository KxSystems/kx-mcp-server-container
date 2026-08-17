"""Capability check: the ``@authorize`` decorator + its config + audit hook.

This is the fastmcp-bound half of the capability-check seam (the fastmcp-free decision contract is
:mod:`kx_auth_core.authz`). ``@authorize(action, resource)`` wraps a primitive: inside the tool's
own context it reads :func:`current_principal`, builds an :class:`~kx_auth_core.AuthzRequest`
(``namespace`` = the part of ``resource`` before ``":"`` or ``"."``), and calls
:func:`~kx_auth_core.decide`
against the configured strategy (``KX_MCP_AUTHZ``). A deny raises :class:`AuthorizationDenied`
(rendered by fastmcp as a clean tool error — never a stack trace or a container crash); the
decision is stashed on the :data:`current_authz_decision` contextvar so the parent
:class:`~kx_mcp_core.auth.AuditMiddleware` can fold ``decision`` + ``adapter`` into the one dispatch
audit line.

A decorator, not a parent pre-dispatch middleware, because it runs inside the tool's own context:
``current_principal()`` and (for a data-gate adapter) ``ctx.fastmcp`` config and the bound
connection are all reachable there, whereas a parent middleware can't reach into a mounted child
cleanly. **Decorate selectively** — only where a capability concern (subject × tool-class: write /
admin / a tool category some role may never invoke) is distinct from the backend's own data gate;
otherwise leave the primitive route-only and let the data gate decide.
"""

from __future__ import annotations

import functools
import inspect
from contextvars import ContextVar
from typing import Callable, Optional

from fastmcp.exceptions import ToolError

from kx_auth_core import AuthzDecision, AuthzRequest, decide, register_authz_adapter

from .authz_settings import AuthzSettings
from .principal import current_principal

# Set by the decorator inside the tool; read by AuditMiddleware after the dispatch so one audit line
# carries decision + adapter. None when the dispatched primitive carried no @authorize (route-only).
current_authz_decision: ContextVar[Optional[AuthzDecision]] = ContextVar(
    "current_authz_decision", default=None
)


class AuthorizationDenied(ToolError):
    """A capability-check denial. A ``ToolError`` so fastmcp renders a clean client-facing message
    (the agent reads it and pivots) rather than a stack trace."""


# --- config + adapter registration -------------------------------------------------------------

_SETTINGS: Optional[AuthzSettings] = None


def configure_authz(settings: Optional[AuthzSettings] = None) -> AuthzSettings:
    """Resolve + cache :class:`AuthzSettings` and register the built-in adapter for the configured
    mode. Called by the launcher at startup (so a bad policy file fails loudly there, not
    mid-request) and by tests; the decorator calls it lazily on first use if never configured."""
    global _SETTINGS
    settings = settings or AuthzSettings()
    _SETTINGS = settings
    if settings.mode == "static":
        # Lazy import: only pull in yaml / the adapter when static mode is actually used. Construction
        # loads + validates the policy file, so a bad path raises here (startup), not per request.
        from .capability import StaticCapabilityAdapter

        register_authz_adapter("static", StaticCapabilityAdapter(settings))
    return settings


def _settings() -> AuthzSettings:
    return _SETTINGS if _SETTINGS is not None else configure_authz()


# --- the decorator -----------------------------------------------------------------------------


def authorize(action: str, resource: str) -> Callable:
    """Gate a primitive with a capability check.

    ``resource`` is ``"namespace.thing"`` or ``"namespace:thing"`` by convention; ``namespace``
    (the policy-file key, and ``AuthzRequest.namespace``) is derived as the part before the first
    separator. Applies to sync and async primitives alike; ``functools.wraps`` preserves the
    signature so fastmcp's ``@tool`` introspection is unaffected (place ``@authorize`` *below*
    ``@mcp.tool()``).
    """
    namespace = resource.split(":", 1)[0].split(".", 1)[0]

    def deco(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrap(*args, **kwargs):
                _check(action, resource, namespace)
                return await fn(*args, **kwargs)

            return awrap

        @functools.wraps(fn)
        def swrap(*args, **kwargs):
            _check(action, resource, namespace)
            return fn(*args, **kwargs)

        return swrap

    return deco


def _check(action: str, resource: str, namespace: str) -> None:
    """Build the request, decide, record the decision, and deny-by-raise. Raises only
    :class:`AuthorizationDenied` (a clean tool error); an adapter that itself raises is turned into a
    fail-closed deny by :func:`~kx_auth_core.decide`, so this never leaks a backend stack trace."""
    settings = _settings()
    token = current_principal()
    claims = dict(getattr(token, "claims", None) or {})
    subject = claims.get("sub") or (token.client_id if token else None) or "anonymous"
    request = AuthzRequest(
        subject=subject, action=action, resource=resource, namespace=namespace, claims=claims
    )
    decision = decide(request, strategy=settings.mode)
    current_authz_decision.set(decision)
    if not decision.allowed:
        suffix = f" ({decision.reason})" if decision.reason else ""
        raise AuthorizationDenied(f"not authorized: {action} on {resource}{suffix}")
