"""kdb-x capability authorization adapter.

Checks whether a caller may invoke a tool — an MCP-level capability such as running a SQL
query — as distinct from whether they may read the underlying data. The decision is delegated
to the q backend's ``.kx.auth`` policy engine, called with an action/resource pair such as
``(`query;`kdbx.sql)``.

The same engine also enforces the data-level gate (e.g. ``(`read;`data.trades)``). The host's single
``setPolicy`` routes by resource vocabulary — ``kdbx.sql`` to the capability grants and
``data.<table>`` to the data grants — so one engine serves both concerns from two grant sets.

``rbac_check`` is the adapter, registered as ``"kdbx_rbac"``. The ``@authorize`` decorator on the
SQL tool routes here (via :func:`kx_auth_core.authz.decide`) when the container runs with
``KX_MCP_AUTHZ=kdbx_rbac``. The SQL tool does not import this module, so ``server.py`` imports it
so its registration runs.
"""

from __future__ import annotations

import logging
from typing import Union

import pykx as kx
from kx_auth_core.authz import AuthzDecision, AuthzRequest, register_authz_adapter

from kx_mcp_kdbx.utils.denial import is_denial

logger = logging.getLogger(__name__)


def rbac_check(conn: kx.QConnection, action: str, resource: str):
    """Consult the q ``.kx.auth`` policy engine for an ``(action;resource)`` decision.

    Calls ``.kx.auth.authorize[action;resource]`` on the bound per-principal handle ``conn``.
    Returns the q principal dict on allow; on deny the q side signals ``'denied: ...``, which PyKX
    raises as a ``QError`` for the caller to map to a structured ``permission_denied`` (via
    :func:`kx_mcp_kdbx.utils.denial.denial_response`, sharing the ``denied:``-prefix convention
    through :func:`~kx_mcp_kdbx.utils.denial.is_denial`).

    ``action`` and ``resource`` are sent as q symbols (``kx.SymbolAtom``) so the policy can compare
    them directly; a symbol carries a dot (e.g. ``kdbx.sql``) unchanged.
    """
    return conn(".kx.auth.authorize", kx.SymbolAtom(action), kx.SymbolAtom(resource))


def _resolve_bound_conn() -> kx.QConnection:
    """Resolve the bundle's bound per-principal handle from the *live request context*.

    Ctx-resolved, not captured in a closure over ``config``: the :mod:`kx_auth_core.authz` registry
    is one process-global dict, so a single ``"kdbx_rbac"`` registration serves every mounted kdb-x
    backend. Baking a config in would let a second mount clobber the first's (breaking
    instance-safety). Instead the adapter reads each call's own ``ctx.fastmcp._kdbx_config`` and lets
    :func:`get_kdb_connection` bind the current principal. ``get_context`` is imported lazily so
    importing this module never requires a running request.
    """
    from fastmcp.server.dependencies import get_context

    from kx_mcp_kdbx.utils.kdbx import config_from_ctx, get_kdb_connection

    return get_kdb_connection(config_from_ctx(get_context()))


def kdbx_rbac_adapter(request: AuthzRequest) -> Union[bool, AuthzDecision]:
    """Adapter that delegates the ``(action;resource)`` decision to q ``.kx.auth``.

    Resolves the bound handle from the request context (see :func:`_resolve_bound_conn`), runs the q
    policy call, and normalises the outcome onto the contract's boolean-first shape:

    * allow → ``True`` (``decide`` stamps the adapter name).
    * q denial (``'denied: ...``) → ``AuthzDecision(allowed=False, reason=...)``, so the reason
      reaches the audit line.
    * anything else (backend unreachable, malformed response) → re-raise, so ``decide`` fails closed
      and the policy-deny-vs-infrastructure-error distinction is preserved.
    """
    conn = _resolve_bound_conn()
    try:
        rbac_check(conn, request.action, request.resource)
    except Exception as exc:
        if is_denial(exc):
            return AuthzDecision(allowed=False, reason=str(exc).strip())
        raise  # not a policy deny — let decide() fail closed (infrastructure error)
    return True


# Self-register at import (mirrors the outbound built-ins in kx_auth_core.outbound.strategies).
# Registration is inert until something calls decide(request, strategy="kdbx_rbac") — the caller is
# the @authorize(action="query", resource="kdbx.sql") decorator on the SQL tool, active under
# KX_MCP_AUTHZ=kdbx_rbac. The SQL tool does not import this module, so server.py imports it for this
# registration side effect. The `query`/`kdbx.sql` vocabulary is the convention this adapter and the
# q-side capability grant set (`.kx.auth` setPolicy) both key on.
register_authz_adapter("kdbx_rbac", kdbx_rbac_adapter)
