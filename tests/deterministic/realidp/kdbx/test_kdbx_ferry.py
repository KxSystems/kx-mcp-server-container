"""kdbx "ferry" strategy — live proof against a real q process + real Keycloak (M4/M5).

Exercises the full chain, with no mocking:

    MCP client --(Bearer Keycloak-token)--> container [KX_MCP_AUTH=jwks, quants realm]
        --> kdbx tool --(assert_identity: .kx.auth.bind)--> real q
            --> .kx.auth.authorize[`read;`trades] (`trader` group grant, default-deny)

Before this test, the kx.auth module's bind/authorize/setPolicy logic had automated coverage only
in isolation (`test_kx_auth_assertion_gate.py`, a hand-built principal dict, no container, no
PyKX) or via a manual bash demo (`demos/M4-identity-assertion/run.sh`). This is the first
automated proof of the real chain: container -> PyKX -> qIPC bind -> q authorize -> a structured
tool response.

Reuses the real live Keycloak `quants` realm alice/bob already use in `idp`/`kdbai` — alice has
the `trader` group, bob does not. Fixtures `kdbx_ferry_host`/`kdbx_ferry_container_url` come from
`realidp/kdbx/conftest.py`; `alice_token`/`bob_token` are inherited from the shared root
`realidp/conftest.py`.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

QUERY = "SELECT sym, price, size FROM trades WHERE sym = 'AAPL'"


# ---------------------------------------------------------------------------
# alice (trader) — bind + promote + authorize all hold
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbx
def test_kdbx_ferry_alice_in_trader_group_allowed(kdbx_ferry_container_url, alice_token):
    """alice's real Keycloak token carries `groups:["trader",...]`; her query succeeds (M4/M5).

    Proves the full chain: the container validates her inbound bearer, projects + ferries the
    principal via .kx.auth.bind on the (service-account) qIPC handle, and q's .s.e wrapper calls
    .kx.auth.authorize[`read;`trades] — which the `trader` group grant allows.
    """
    async def go() -> dict:
        async with Client(StreamableHttpTransport(kdbx_ferry_container_url, auth=alice_token)) as c:
            result = await c.call_tool("kdbx_run_sql_query", {"query": QUERY})
            return result.data

    data = asyncio.run(go())
    assert data.get("status") == "success", f"expected alice to succeed, got: {data!r}"
    assert data.get("data"), f"expected non-empty rows for alice, got: {data!r}"


# ---------------------------------------------------------------------------
# bob (viewer only) — bound, but ungranted -> structured permission_denied
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbx
def test_kdbx_ferry_bob_without_trader_group_denied(kdbx_ferry_container_url, bob_token):
    """bob's real Keycloak token has no `trader` group; his query is denied at the q layer (M5).

    bob's principal IS bound (the service-account connection is trusted to assert any identity —
    the bind-gate gates the *caller*, not the asserted principal), but .kx.auth.authorize refuses
    because his groups don't match the `trader` grant. The q-side `'denied: ...` signal is
    translated into a clean structured envelope (denial.py), not a raw stack trace.
    """
    async def go() -> dict:
        async with Client(StreamableHttpTransport(kdbx_ferry_container_url, auth=bob_token)) as c:
            result = await c.call_tool("kdbx_run_sql_query", {"query": QUERY})
            return result.data

    data = asyncio.run(go())
    assert data.get("status") == "error", f"expected bob to be denied, got: {data!r}"
    assert data.get("error_type") == "permission_denied", f"expected permission_denied, got: {data!r}"


# ---------------------------------------------------------------------------
# unbound connection — default-deny holds independent of the container's own plumbing
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbx
def test_kdbx_ferry_unbound_connection_denied_by_default(kdbx_ferry_host):
    """A raw qIPC connection that never calls .kx.auth.bind is denied by default (M4).

    Opens a second connection directly against the ferry host, using the SAME service-account
    credentials the container connects with, but bypassing the container entirely — .kx.auth.bind
    is never called on this handle. Proves the q module's own default-deny holds on its own
    terms, not merely because the container happens to always bind before querying.
    """
    import pykx as kx

    host, port, svc_user, svc_password = kdbx_ferry_host
    conn = kx.SyncQConnection(host=host, port=port, username=svc_user, password=svc_password, timeout=5)
    try:
        # PyKX converts a Python str -> q symbol, so "read"/"trades" arrive as `read/`trades.
        with pytest.raises(Exception) as exc_info:
            conn(".kx.auth.authorize", "read", "trades")
        assert "denied" in str(exc_info.value).lower(), (
            f"expected a q-side 'denied signal, got: {exc_info.value!r}"
        )
    finally:
        conn.close()
