"""The pluggable authorization-decision seam.

``decide(request, *, strategy)`` resolves ``strategy`` from a registry and runs the registered
adapter, always returning an :class:`AuthzDecision` so the tool path reads ``allowed`` / ``reason`` /
``obligations`` uniformly. A registry (not a hard-coded conditional) so a backend's capability
adapter slots in via :func:`register_authz_adapter` without touching dispatch.
Shaped like the outbound seam in :mod:`kx_auth_core.outbound.strategies`
(``register_outbound_strategy`` / ``exchange`` / ``outbound_strategies``), so the codebase has one
extension pattern, not two.

This registry serves two kinds of check. A **capability check** sits at the tool boundary —
data-agnostic, driven by the ``@authorize`` decorator (wired in :mod:`kx_mcp_core`), which builds an
:class:`AuthzRequest` and calls :func:`decide`. An adapter can also do explicit-consult: call a
backend's own data gate as a policy point and act on its verdict — the kdb-x entitlements adapter
(``kdbx_entitlements``, consulting q ``.kx.auth.entitled`` in-tool on the bound handle) is the first
of these. A backend whose data gate enforces directly on the data call itself (for example KDB.AI ACL)
needs no adapter here. The bridge between the two check styles is :attr:`AuthzDecision.obligations`:
a free-form scope-down payload (an entitled-symbol list, a where-clause fragment) an adapter uses to
answer *allow-with-obligations* through the same boolean-first shape.

Deliberately fastmcp-free. The ``action`` / ``resource`` vocabulary is a documented convention, not
validated here (mirrors how the outbound seam never validates its audience/resource strings) — an
adapter and the backend's own data gate must agree on the same strings by convention, not by code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Protocol, Union, runtime_checkable

# The strategy values that mean "no adapter — route-only allow" (the inbound `unset` precedent):
# authorization is opt-in, so an unconfigured seam must not regress the zero-config posture.
_ROUTE_ONLY = frozenset({"", "unset", "none"})


@dataclass(frozen=True)
class AuthzRequest:
    """The question a capability check answers: may ``subject`` perform ``action`` on ``resource``?

    ``action`` (e.g. ``query`` / ``write`` / ``search`` / ``admin``) and ``resource`` (e.g.
    ``kdbx.sql`` / ``kdbai:table`` / ``acme:package``) are a documented convention, not validated —
    both this adapter and the q ``authorize[action;resource]`` gate key on the same strings.
    ``namespace`` is the mount namespace the primitive lives under (``kdbx`` / ``kdbai`` / ``acme``).
    ``claims`` is the validated inbound principal's raw claims (an audit/escape-hatch payload;
    adapters should prefer the promoted fields a backend exposes over re-deriving from raw claims).
    """

    subject: str
    action: str
    resource: str
    namespace: str
    claims: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthzDecision:
    """The answer slip :func:`decide` always returns. Boolean-first; ``obligations`` is the optional
    scope-down extension.

    ``allowed`` is the decision. ``adapter`` records which registered adapter produced it (stamped by
    :func:`decide`; ``None`` for the route-only allow). ``reason`` is a human/audit string. The
    default-empty ``obligations`` is where an adapter expresses *allow-with-obligations* — a free-form
    payload the tool/backend applies to scope a result down (e.g. an entitled-symbol list, a
    where-clause fragment). Most adapters return a bare ``bool`` and never touch it.
    """

    allowed: bool
    adapter: Optional[str] = None
    reason: Optional[str] = None
    obligations: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class AuthzAdapter(Protocol):
    """A capability adapter. Registered via :func:`register_authz_adapter` and dispatched by
    :func:`decide`. Receives the :class:`AuthzRequest` and returns ``True`` / ``False`` for the common
    case, or a full :class:`AuthzDecision` to carry ``reason`` / ``obligations``. **Raise to fail
    closed** — :func:`decide` turns any exception into a deny, never an allow.
    """

    def __call__(self, request: AuthzRequest) -> Union[bool, AuthzDecision]: ...


_ADAPTERS: dict[str, AuthzAdapter] = {}


def register_authz_adapter(name: str, adapter: AuthzAdapter) -> None:
    """Register an authorization adapter under ``name``. The extension point for a backend's
    capability check. Mirrors ``register_outbound_strategy``."""
    _ADAPTERS[name] = adapter


def authz_adapters() -> list[str]:
    """The names of all registered adapters, sorted. Mirrors ``outbound_strategies()`` / ``auth_modes()``."""
    return sorted(_ADAPTERS)


def decide(request: AuthzRequest, *, strategy: Optional[str]) -> AuthzDecision:
    """Resolve ``strategy`` to an adapter, run it, and return an :class:`AuthzDecision`.

    Three postures — the easy thing to get wrong, so they are explicit here:

    * **route-only** — ``strategy`` is unset / ``"unset"`` / ``"none"`` (or ``None``): **allow**, with
      ``adapter=None``. Authorization is opt-in; an unconfigured seam must not regress the zero-config
      single-principal posture (the inbound ``KX_MCP_AUTH=unset`` precedent).
    * **unknown strategy** — a *named* strategy not in the registry: ``ValueError`` (a clear operator
      config error, surfaced loudly — mirrors ``exchange``'s unknown-strategy).
    * **adapter raised** — a registered adapter throws at runtime: **deny / fail closed**
      (``allowed=False``), the exception captured in ``reason``. An adapter exception must *never*
      fall through to allow (matches the q-side ``policy:{[…] 0b}`` default).

    A registered adapter returning a bare ``bool`` is normalised into an :class:`AuthzDecision`;
    returning an :class:`AuthzDecision` is passed through with ``obligations`` intact. Either way the
    producing ``adapter`` is stamped, so the caller reads one uniform shape.
    """
    if not strategy or strategy in _ROUTE_ONLY:
        return AuthzDecision(allowed=True, adapter=None, reason="no adapter configured (route-only)")

    try:
        adapter = _ADAPTERS[strategy]
    except KeyError:
        raise ValueError(
            f"unknown authz strategy {strategy!r}; known: {authz_adapters()}"
        ) from None

    try:
        result = adapter(request)
    except Exception as exc:  # fail closed — an adapter error is a deny, never an allow
        return AuthzDecision(
            allowed=False, adapter=strategy, reason=f"adapter raised: {type(exc).__name__}: {exc}"
        )

    if isinstance(result, AuthzDecision):
        # Pass the decision through (obligations intact), stamping the adapter when it didn't.
        return result if result.adapter else replace(result, adapter=strategy)
    return AuthzDecision(allowed=bool(result), adapter=strategy)
