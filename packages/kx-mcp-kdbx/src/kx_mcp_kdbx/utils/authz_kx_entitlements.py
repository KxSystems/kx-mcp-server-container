"""kdb-x data-gate adapter — the entitlement consult used by the SQL tool and table-listing resource.

Where :mod:`authz_kx_rbac` answers *may this subject invoke the tool at all?* (an MCP-semantic
``(action;resource)`` capability check like ``(`query;`kdbx.sql)``), this module answers a
*data-semantic* question: *of the data you asked for, what may you see?* It puts that question to
the same q ``.kx.auth`` engine on the **bound per-principal handle**, via the one-round-trip
``.kx.auth.entitled[action;resources]`` (the scope-down companion to ``authorize``). It is driven
**in-tool** — after ``get_kdb_connection`` has bound the principal — never from parent middleware,
which would hit ``require[]``'s default-deny pre-bind.

Three verdict shapes, per the S/A/R contract (:mod:`kx_auth_core.authz`):

* **allow** — every consulted table is entitled → ``True``.
* **deny** — none is → ``AuthzDecision(allowed=False, reason=…)``.
* **scope-down** — a strict subset is → ``AuthzDecision(allowed=True, obligations={"entitled": […],
  "denied": […]})``. The *caller* applies the obligation: the table-listing resource filters its
  output to the entitled subset; the SQL tool — which must not rewrite a query — surfaces it as a
  denial-with-guidance naming the entitled subset so the agent can re-scope.

**Resource derivation is best-effort, not a security boundary.** :func:`derive_tables` intersects
the query's identifier tokens with the backend's ``tables[]`` — a false negative (a table reached
through an alias or construction the tokenizer misses) bypasses only *this container-side* consult;
the q-side gate (the host's ``.s.e`` wrap, where wired) still backstops. A false positive (a table
name inside a string literal) fails *safe* — it can only narrow, never leak.

Registered under ``"kdbx_entitlements"`` on the same :mod:`kx_auth_core.authz` registry as the
capability-check adapters, but it is **not** selected by ``KX_MCP_AUTHZ`` (that selects the
capability strategy): the tool path drives it directly through :func:`consult_data_gate` when the
per-backend ``KDBX_DB_DATA_GATE`` flag is on, so two mounted kdb-x backends can gate independently.
Like ``kdbx_rbac`` it is ctx-resolved, not closure-over-config (instance-safety), and ``server.py``
imports this module for the registration side effect.
"""

from __future__ import annotations

import logging
import re
from typing import Sequence, Union

import pykx as kx
from kx_auth_core.authz import AuthzDecision, AuthzRequest, decide, register_authz_adapter

from kx_mcp_kdbx.utils.authz_kx_rbac import _resolve_bound_conn
from kx_mcp_kdbx.utils.denial import is_denial
from kx_mcp_kdbx.utils.kdbx import _current_principal

logger = logging.getLogger(__name__)

ADAPTER_NAME = "kdbx_entitlements"

# The canonical q-facing namespace for physical table resources. Keep this distinct from
# RESOURCE_PREFIX, which is the container-internal encoding used to carry a set of physical table
# names through AuthzRequest before the adapter consults q.
Q_DATA_RESOURCE_PREFIX = "data."

# The resource convention for a data-gate consult: the derived table set rides in the
# AuthzRequest's resource string as `kdbx:data:<t1>,<t2>,...` (action/resource vocabulary is a
# convention, not validated). Table names cannot contain a comma, so the join is unambiguous.
RESOURCE_PREFIX = "kdbx:data:"

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def derive_tables(query: str, known_tables: Sequence[str]) -> list[str]:
    """The tables a SQL query references: its identifier tokens ∩ the backend's ``tables[]``.

    Case-sensitive exact match (q table names are case-sensitive). Best-effort by design — see the
    module docstring for the false-negative/false-positive posture. Sorted so the derived set (and
    the resource string built from it) is deterministic.
    """
    tokens = set(_IDENTIFIER.findall(query))
    return sorted(t for t in known_tables if t in tokens)


def tables_from_resource(resource: str) -> list[str]:
    """Split a ``kdbx:data:<t1>,<t2>`` resource string back into its table list ([] if not ours)."""
    if not resource.startswith(RESOURCE_PREFIX):
        return []
    return [t for t in resource[len(RESOURCE_PREFIX):].split(",") if t]


def q_resource_for_table(table: str) -> str:
    """Map a physical table name to the canonical q-facing data resource."""
    return f"{Q_DATA_RESOURCE_PREFIX}{table}"


def entitled_check(conn: kx.QConnection, action: str, resources: Sequence[str]) -> list[str]:
    """Consult q ``.kx.auth.entitled[action;resources]`` — the allowed subset, one round-trip.

    ``resources`` are physical table names used by the SQL/tool layer. They are sent to q as
    ``data.<table>`` symbols, keeping the grant vocabulary distinct from q's physical namespace,
    and the returned resource symbols are mapped back to the original table names. Unexpected q
    results are ignored so a malformed or over-broad backend response cannot confer access.

    Raises the q ``'denied: ...`` signal when no valid principal is bound (require[]'s default-deny)
    — the caller maps it; any other error is an infrastructure failure the adapter re-raises so
    ``decide()`` fails closed.
    """
    resource_to_table = {q_resource_for_table(table): table for table in resources}
    result = conn(
        ".kx.auth.entitled",
        kx.SymbolAtom(action),
        kx.SymbolVector(list(resource_to_table)),
    )
    allowed_resources = {str(resource) for resource in result.py()}
    return [
        table
        for resource, table in resource_to_table.items()
        if resource in allowed_resources
    ]


def kdbx_entitlements_adapter(request: AuthzRequest) -> Union[bool, AuthzDecision]:
    """Data-gate adapter: consult the q data gate for the request's derived table set.

    Verdict mapping (see the module docstring): all entitled → ``True``; none → deny with a reason
    naming the refused tables; a strict subset → **allow-with-obligations** (``entitled`` /
    ``denied`` lists — the scope-down payload the caller applies). A q ``'denied:`` (unbound or
    expired principal) → deny with the q reason; anything else re-raises so ``decide()`` fails
    closed. A resource carrying no tables allows: there is nothing to consult (the tool skips the
    gate for such queries anyway — recorded here so a direct ``decide()`` call agrees).
    """
    tables = tables_from_resource(request.resource)
    if not tables:
        return True
    conn = _resolve_bound_conn()
    try:
        entitled = entitled_check(conn, request.action, tables)
    except Exception as exc:
        if is_denial(exc):
            return AuthzDecision(allowed=False, reason=str(exc).strip())
        raise  # not a policy deny — let decide() fail closed (infrastructure error)
    denied = [t for t in tables if t not in set(entitled)]
    if not denied:
        return True
    refused = ", ".join(denied)
    if not entitled:
        return AuthzDecision(
            allowed=False,
            reason=f"{request.subject} not permitted {request.action} on {refused}",
        )
    return AuthzDecision(
        allowed=True,
        reason=f"scoped down: {request.subject} not permitted {request.action} on {refused}",
        obligations={"entitled": entitled, "denied": denied},
    )


def consult_data_gate(action: str, tables: Sequence[str]) -> AuthzDecision:
    """Drive one data-gate consult through the S/A/R seam and record it for the audit line.

    The in-tool entry point (the ``@authorize`` decorator cannot drive this — its action/resource
    are static at decoration time, while the table set is per-query). Builds the ``AuthzRequest``
    (same subject derivation as the decorator: ``sub`` claim → client id → ``"anonymous"``), runs
    ``decide(strategy="kdbx_entitlements")`` — inheriting its fail-closed + adapter-stamping
    posture — and stashes the decision on ``current_authz_decision`` so the parent AuditMiddleware
    folds ``decision`` + ``adapter`` into the dispatch's audit record. On a dispatch that already
    ran a capability check the slot is overwritten (last-writer-wins): the capability check ran
    first and only an *allow* reaches this consult, so the line carries the decision that determined
    the outcome.
    """
    principal = _current_principal()
    claims = dict(getattr(principal, "claims", None) or {})
    subject = claims.get("sub") or getattr(principal, "client_id", None) or "anonymous"
    request = AuthzRequest(
        subject=subject,
        action=action,
        resource=RESOURCE_PREFIX + ",".join(tables),
        namespace="kdbx",
        claims=claims,
    )
    decision = decide(request, strategy=ADAPTER_NAME)
    try:
        from kx_mcp_core.auth import current_authz_decision

        current_authz_decision.set(decision)
    except ImportError:  # standalone bundle posture — no container, no audit middleware
        pass
    return decision


# Self-register at import (mirrors authz_kx_rbac / the outbound built-ins). Inert until a tool with
# KDBX_DB_DATA_GATE=true drives it via consult_data_gate(); server.py imports this module for the
# side effect.
register_authz_adapter(ADAPTER_NAME, kdbx_entitlements_adapter)
