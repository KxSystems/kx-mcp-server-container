"""KDB.AI OAuth ACL integration tests — TESTING.md rows KA.1–KA.7.

Exercises the full passthrough chain:

    MCP client --(Bearer OIDC token)--> container [KX_MCP_AUTH=jwks, kdbai-service Keycloak]
        --> kdbai tool --(passthrough: bearer as qipc password)--> OAuth kdbai-db
            --> ACL check on propagated identity (tenant + groups claims)

Personas:
    alice  quants / [trader, viewer]  — has the trader DB-read grant on db_read
    bob    quants / [viewer]          — no grant on db_read (viewer only, no trader grant)
    root   manager / [admin]          — system_admin, bypasses all ACL checks

Markers:
    @pytest.mark.realidp  — excluded from just test (default CI run)
    @pytest.mark.kdbai    — subset: just test-kdbai (sources envs/.env.kdbai)

Prerequisites (one-time, operator pre-step — see keycloak/README for details):
    1. docker compose --profile backends up -d
    2. uv run python keycloak_setup.py keycloak_config.json
    3. uv run python seed.py
    4. source envs/.env.kdbai
    5. just test-kdbai

Grants seeded (from seed.py):
    quants/trader  → DB-level read on db_read        (alice can; bob cannot)
    quants/viewer  → table-level read on db_read_isolated/T1 only  (KA.6 no-bleed)
    quants/service → DB-level read on db_read        (KA.8/KA.9 — the service_account
                     machine identity's own grant, independent of any human persona's)

KA.8/KA.9 exercise the ``service_account`` outbound strategy (row 3.9) instead of
passthrough: the container mints its own client-credentials token (kdbai-service-worker)
rather than forwarding the caller's bearer. See ``kdbai_service_account_container_url``
in conftest.py.

TEST_REFERENCE: KA.1 (row 5.3 preamble) through KA.9.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from realidp.kdbai.helpers import (
    assert_denied,
    assert_no_visible_tables,
    assert_permitted,
    assert_visible_tables,
)

# kdbai_container_url, alice_token, bob_token, root_token injected by realidp/kdbai/conftest.py


# ---------------------------------------------------------------------------
# KA.1 — kdbai bundle mounts and advertises its tools
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_bundle_mounts(kdbai_container_url, alice_token):
    """kdbai bundle mounts against a real OAuth kdbai-db and advertises its tools (KA.1).

    The container starting at all proves the kdbai passthrough pre-flight passed
    (kdbai-db reachable with alice's bearer). Listing tools and finding the expected
    names proves the bundle mounted under the ``kdbai`` namespace correctly.
    """
    async def go() -> set:
        async with Client(StreamableHttpTransport(kdbai_container_url, auth=alice_token)) as c:
            return {t.name for t in await c.list_tools()}

    tools = asyncio.run(go())
    assert "kdbai_list_tables" in tools
    assert "kdbai_query_data" in tools
    assert "kdbai_list_databases" in tools
    assert "kdbai_table_info" in tools


# ---------------------------------------------------------------------------
# KA.2 — two-persona DB-read differentiation  (row 5.3 headline)
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_acl_list_tables_differentiates_personas(kdbai_container_url, alice_token, bob_token):
    """kdbai_list_tables: alice (trader) sees T1; bob (viewer-only) sees [] (KA.2).

    alice is in quants/[trader,viewer]; bob is in quants/[viewer] only. The seed
    grant is quants/trader → DB-level read on db_read. alice's token propagated
    via passthrough lets her see T1; bob's sees an empty table list. This is the
    committed version of the 2026-06-18 live validation result.
    """
    async def go(token) -> dict:
        async with Client(StreamableHttpTransport(kdbai_container_url, auth=token)) as c:
            return (await c.call_tool("kdbai_list_tables", {"database_name": "db_read"})).data

    alice_result = asyncio.run(go(alice_token))
    bob_result = asyncio.run(go(bob_token))

    assert_visible_tables(alice_result, "T1")
    assert_no_visible_tables(bob_result)


# ---------------------------------------------------------------------------
# KA.3 — denied operation surfaces as a structured error dict
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_acl_query_data_permitted_and_denied(kdbai_container_url, alice_token, bob_token):
    """kdbai_query_data: alice (trader) succeeds; bob (viewer) gets status:error (KA.3).

    Confirms that kdbai-db ACL denials surface as ``{"status":"error","message":"..."}``
    in the tool result — the container translates the exception into a structured error
    response rather than letting the request fail with an HTTP error.
    """
    async def go(token) -> dict:
        async with Client(StreamableHttpTransport(kdbai_container_url, auth=token)) as c:
            return (await c.call_tool("kdbai_query_data", {"table_name": "T1", "database_name": "db_read"})).data

    alice_result = asyncio.run(go(alice_token))
    bob_result = asyncio.run(go(bob_token))

    assert_permitted(alice_result)
    assert_denied(bob_result)


# ---------------------------------------------------------------------------
# KA.4 — table_info under read grant  (analog of AU-R-06 getTable)
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_acl_table_info_permitted_and_denied(kdbai_container_url, alice_token, bob_token):
    """kdbai_table_info: alice (trader) gets schema + stats; bob (viewer) gets status:error (KA.4).

    Exercises the table-level read path without requiring an embedding provider —
    table_info calls kdbai-db's metadata endpoint directly. alice's propagated bearer
    is granted; bob's is not.
    """
    async def go(token) -> dict:
        async with Client(StreamableHttpTransport(kdbai_container_url, auth=token)) as c:
            return (await c.call_tool("kdbai_table_info", {"table_name": "T1", "database_name": "db_read"})).data

    alice_result = asyncio.run(go(alice_token))
    bob_result = asyncio.run(go(bob_token))

    assert_permitted(alice_result)
    assert_denied(bob_result)


# ---------------------------------------------------------------------------
# KA.5 — listDatabases is system_admin-only
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_acl_list_databases_system_admin_only(
    kdbai_container_url, kdbai_manager_container_url, alice_token, root_token
):
    """kdbai_list_databases: root (system_admin) sees the list; alice (trader) is denied (KA.5).

    root is in manager/[admin] — kdbai-db's system_admin identity, which bypasses
    all ACL checks. alice is in quants/[trader,viewer] with a DB-read grant, but
    listDatabases is a system_admin operation; non-admins are denied.

    root's manager-realm token cannot pass through the quants-only container gate, so
    this test uses a second container (``kdbai_manager_container_url``) configured to
    accept manager-realm tokens for the system_admin assertion. alice's quants-realm
    token goes to the main ``kdbai_container_url``.

    Also asserts that root can ``query_data`` on ``db_read/T1`` — a table that has a
    ``quants/trader`` grant, not a ``manager`` grant — proving system_admin bypass is
    genuine cross-tenant authorization, not just database-list visibility.
    """
    async def go(url, token, tool: str, params: dict) -> dict:
        async with Client(StreamableHttpTransport(url, auth=token)) as c:
            return (await c.call_tool(tool, params)).data

    root_db_list = asyncio.run(
        go(kdbai_manager_container_url, root_token, "kdbai_list_databases", {})
    )
    root_query = asyncio.run(
        go(kdbai_manager_container_url, root_token, "kdbai_query_data",
           {"table_name": "T1", "database_name": "db_read"})
    )
    alice_db_list = asyncio.run(
        go(kdbai_container_url, alice_token, "kdbai_list_databases", {})
    )

    assert_permitted(root_db_list)
    assert root_db_list.get("databases") is not None, (
        f"root should see a databases list but got: {root_db_list!r}"
    )
    assert_permitted(root_query)  # system_admin reads a quants-granted table without any manager grant
    assert_denied(alice_db_list)


# ---------------------------------------------------------------------------
# KA.6 — table-scoped grant does not bleed to other tables
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_acl_table_scoped_grant_no_bleed(kdbai_container_url, alice_token):
    """Table-scoped grant on db_read_isolated/T1: alice can query T1 but not T2 (KA.6).

    The seed grant is quants/viewer → table-level read on T1 only. alice is in
    viewer, so she can query T1. T2 has no grant — querying it returns status:error
    even though alice has a valid grant for a different table in the same database.
    This is the committed analog of AU-R-11.
    """
    async def go(table_name: str) -> dict:
        async with Client(StreamableHttpTransport(kdbai_container_url, auth=alice_token)) as c:
            return (await c.call_tool(
                "kdbai_query_data",
                {"table_name": table_name, "database_name": "db_read_isolated"},
            )).data

    t1_result = asyncio.run(go("T1"))
    t2_result = asyncio.run(go("T2"))

    assert_permitted(t1_result)
    assert_denied(t2_result)


# ---------------------------------------------------------------------------
# KA.7 — no bearer at all → 401 at the inbound gate
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_no_bearer_rejected(kdbai_container_url):
    """No auth header → 401 before any kdbai-db call is made (KA.7).

    Tests the container's HTTP-layer auth check in isolation — does not require
    a token or a live kdbai-db response. Works even when the kdbai-db is
    temporarily unavailable, as the rejection fires before tool dispatch.
    """
    async def go() -> None:
        async with Client(StreamableHttpTransport(kdbai_container_url)) as c:
            await c.call_tool("kdbai_list_tables", {"database_name": "db_read"})

    with pytest.raises(Exception) as exc_info:
        asyncio.run(go())
    err = str(exc_info.value)
    assert "401" in err or "Unauthorized" in err, (
        f"Expected 401 Unauthorized but got: {err!r}"
    )


# ---------------------------------------------------------------------------
# KA.8/KA.9 — service_account outbound strategy (row 3.9): the container's own
# machine identity, not the caller's, is what reaches kdbai-db.
# ---------------------------------------------------------------------------


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_service_account_query_succeeds(kdbai_service_account_container_url, alice_token):
    """A real client_credentials grant against live Keycloak mints a token kdbai-db accepts (KA.8).

    The seed grant is quants/service → DB-level read on db_read — independent of alice's own
    quants/trader grant, so this cannot pass by accidentally inheriting her ACL. A human bearer
    (alice's) is still required to reach the tool at all — the inbound gate is unchanged — but
    what reaches kdbai-db is the container's own kdbai-service-worker machine token.
    """
    async def go() -> dict:
        async with Client(StreamableHttpTransport(kdbai_service_account_container_url, auth=alice_token)) as c:
            return (await c.call_tool("kdbai_list_tables", {"database_name": "db_read"})).data

    result = asyncio.run(go())
    assert_visible_tables(result, "T1")


@pytest.mark.realidp
@pytest.mark.kdbai
def test_kdbai_service_account_result_independent_of_caller(
    kdbai_service_account_container_url, alice_token, bob_token
):
    """alice and bob get an *identical* result under service_account (KA.9).

    Under passthrough (KA.2/KA.3), alice (trader) and bob (viewer-only) get different results
    because kdbai-db enforces on each human's own propagated claims. Under service_account the
    backend never sees either human's claims at all — only the container's machine identity — so
    if the two calls produced different results, that would mean a human's identity is leaking
    into the outbound call somewhere. Both succeed and see the same table, purely by virtue of the
    quants/service grant.
    """
    async def go(token) -> dict:
        async with Client(StreamableHttpTransport(kdbai_service_account_container_url, auth=token)) as c:
            return (await c.call_tool("kdbai_list_tables", {"database_name": "db_read"})).data

    alice_result = asyncio.run(go(alice_token))
    bob_result = asyncio.run(go(bob_token))

    assert_visible_tables(alice_result, "T1")
    assert_visible_tables(bob_result, "T1")
    assert alice_result == bob_result, (
        f"Expected identical results regardless of caller — got alice={alice_result!r} "
        f"bob={bob_result!r}"
    )
