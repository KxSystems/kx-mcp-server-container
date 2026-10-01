"""Capability check: the ``@authorize`` decorator + its config + audit hook.

This is the fastmcp-bound half of the capability-check seam (the fastmcp-free decision contract is
:mod:`kx_auth_core.authz`). ``@authorize(action, resource)`` wraps a primitive: inside the tool's
own context it reads :func:`current_principal`, builds an :class:`~kx_auth_core.AuthzRequest`
(``namespace`` = the part of ``resource`` before ``":"`` or ``"."``), and calls
:func:`~kx_auth_core.decide`
against the configured strategy (``KX_MCP_AUTHZ``). A deny raises :class:`AuthorizationDenied`
(rendered by fastmcp as a clean tool error — never a stack trace or a container crash); the
decision is stashed on the :func:`authz_decision` (the ``AuthzSlot``) so the parent
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
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Callable, Optional

from fastmcp.exceptions import ToolError

from kx_auth_core import (
    ROUTE_ONLY_MODES,
    AuthzDecision,
    AuthzRequest,
    authz_adapters,
    decide,
    register_authz_adapter,
)

from .authz_settings import AuthzSettings
from .principal import current_principal, subject_from

@dataclass
class AuthzSlot:
    """One dispatch's holder for its authorization decision. ``None`` = route-only, no check ran.

    **Mutable on purpose, and that is the whole point.** The decision is written inside the tool
    body and read by the parent middleware after the dispatch, and those two can be on different
    threads: fastmcp runs a *sync* tool body via ``anyio.to_thread.run_sync``, which copies the
    context into a worker thread. Copying a context copies each variable's **reference**, so a
    ``ContextVar.set`` inside the copy rebinds only the copy and never reaches the dispatching task
    — the stamp is silently lost and an authorization refusal is misreported as a plain error.
    Mutating a shared object does reach it, because both sides hold the same object.

    Reads across that boundary always worked; only writes were lost. That asymmetry is why
    fastmcp's own "propagates contextvars, safe for functions that depend on context" is true but
    incomplete.
    """

    decision: Optional[AuthzDecision] = None


# Holds the slot, not the decision — see AuthzSlot. Installed per dispatch by AuditMiddleware.
current_authz_slot: ContextVar[Optional[AuthzSlot]] = ContextVar(
    "current_authz_slot", default=None
)


def begin_authz_dispatch() -> Token:
    """Install a **fresh** slot for one dispatch; returns the token to pass to :func:`end_authz_dispatch`.

    Fresh per dispatch, never pooled or module-level: the isolation boundary is the ContextVar
    binding, and a reused slot would move it to object identity instead, letting one dispatch's
    decision leak into the next as a false "already decided" — which would bypass the
    ``decision is None`` route-only check.
    """
    return current_authz_slot.set(AuthzSlot())


def end_authz_dispatch(token: Token) -> None:
    """Restore the previous slot binding, dropping this dispatch's slot."""
    current_authz_slot.reset(token)


def stamp_authz_decision(decision: AuthzDecision) -> None:
    """Record an authorization decision so this dispatch's middleware can read it back.

    **Every writer goes through here** — the ``@authorize`` decorator below, and the backends'
    q-side denial paths (``record_denial`` / ``consult_data_gate``). One place to write means how a
    decision travels from a tool body out to the parent middleware stays a single implementation
    detail rather than something three call sites each encode, and it is what lets the mechanism
    change without touching any caller.

    With no slot installed there is no dispatch scope to report into — the standalone-bundle
    posture, or a direct call in a test. One is created so an in-task read still works, matching
    the previous behaviour; it cannot cross a thread hop, but with no middleware attached there is
    nothing on the other side to read it.
    """
    slot = current_authz_slot.get()
    if slot is None:
        slot = AuthzSlot()
        current_authz_slot.set(slot)
    slot.decision = decision


def authz_decision() -> Optional[AuthzDecision]:
    """This dispatch's decision, or ``None`` for a route-only dispatch. **The only read path.**"""
    slot = current_authz_slot.get()
    return slot.decision if slot is not None else None


class AuthorizationDenied(ToolError):
    """A capability-check denial. A ``ToolError`` so fastmcp renders a clean client-facing message
    (the agent reads it and pivots) rather than a stack trace."""


# --- config + adapter registration -------------------------------------------------------------

_SETTINGS: Optional[AuthzSettings] = None


def _validate_mode(mode: str) -> None:
    """Fail loudly at startup for a named strategy nothing has registered an adapter for.

    The "fails loudly at startup" promise only ever held for ``static``, whose adapter construction
    validates the policy file. Any other named mode — a ``KX_MCP_AUTHZ`` typo, or a strategy whose
    bundle failed to mount and so never ran its registration side effect — was accepted silently
    here and surfaced per-request as ``decide()``'s ``unknown authz strategy`` ValueError: a
    confusing runtime error, from a place that cannot say the config was wrong, on every call.

    ``static`` is exempt because :func:`configure_authz` registers it a few lines below.
    """
    if mode in ROUTE_ONLY_MODES or mode == "static":
        return
    if mode not in authz_adapters():
        raise ValueError(
            f"KX_MCP_AUTHZ={mode!r} has no registered authz adapter. Known: {authz_adapters()}. "
            "A backend registers its adapter when its module is imported, so check the name and "
            "that the bundle providing it is actually mounted."
        )


def configure_authz(
    settings: Optional[AuthzSettings] = None, *, require_adapter: bool = True
) -> AuthzSettings:
    """Resolve + cache :class:`AuthzSettings` and register the built-in adapter for the configured
    mode. Called by the launcher at startup (so a bad policy file fails loudly there, not
    mid-request) and by tests; the decorator calls it lazily on first use if never configured.

    ``require_adapter=False`` defers the "is this mode's adapter registered?" check to a later
    :func:`require_authz_adapter`. The launcher needs that: a backend's adapter (``kdbx_rbac``)
    registers when its bundle is imported, which is after startup config is resolved and before
    anything is served. ``static`` is unaffected: its policy file is still loaded and checked here.
    """
    global _SETTINGS
    settings = settings or AuthzSettings()
    if require_adapter:
        _validate_mode(settings.mode)
    if settings.mode == "static":
        # Lazy import: only pull in yaml / the adapter when static mode is actually used. Construction
        # loads + validates the policy file, so a bad path raises here (startup), not per request.
        from .capability import StaticCapabilityAdapter

        # Construct BEFORE caching the settings: if the policy file is missing or malformed this
        # raises, and a caller that swallows the error must not be left with mode="static" cached
        # while no "static" adapter is registered — every later request would then hit a hard
        # "unknown authz strategy" ValueError instead of a clean deny.
        adapter = StaticCapabilityAdapter(settings)
        register_authz_adapter("static", adapter)
    _SETTINGS = settings
    return settings


def require_authz_adapter(settings: Optional[AuthzSettings] = None) -> None:
    """Raise ``ValueError`` unless the configured mode has a registered adapter.

    The second half of ``configure_authz(require_adapter=False)``: call it once every bundle has
    been imported, so a mode whose provider bundle never loaded (a typo, a failed import) still
    fails at startup. Nothing is served without it, and nothing fails open either way:
    :func:`~kx_auth_core.decide` raises on an unknown strategy.
    """
    _validate_mode((settings or _SETTINGS or AuthzSettings()).mode)


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
    if not namespace:
        # An empty resource, or one with a LEADING separator (":thing", ".thing"), derives an empty
        # namespace — never a real policy key, so every call would silently deny at runtime. This is
        # a config error written in source, so fail where it is written. Raising at decoration time
        # surfaces as a bundle import failure, which try_mount_bundle already handles gracefully:
        # that one backend is disabled with a warning, the container keeps serving the rest.
        raise ValueError(
            f"@authorize(resource={resource!r}) derives an empty namespace — resource must be "
            '"namespace.thing" or "namespace:thing" with a non-empty namespace'
        )

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
    subject = subject_from(token)  # shared with the audit line — one identity, one derivation
    request = AuthzRequest(
        subject=subject, action=action, resource=resource, namespace=namespace, claims=claims
    )
    decision = decide(request, strategy=settings.mode)
    stamp_authz_decision(decision)
    if not decision.allowed:
        suffix = f" ({decision.reason})" if decision.reason else ""
        raise AuthorizationDenied(f"not authorized: {action} on {resource}{suffix}")
