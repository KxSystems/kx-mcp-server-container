"""Container composition proven over the wire against a real launcher subprocess.

The in-process suite (``tests/deterministic/unit/test_composition.py``) already proves
``try_mount_bundle`` disables a failing backend and keeps serving the rest — but only by calling the
assembly seam directly. This tier spawns the launcher as a real ``streamable-http`` child (via the
``spawn_container`` fixture) and drives it with an HTTP MCP client, catching the transport/wiring bug
class in-process tests miss.

X.8 — graceful degradation over the wire: one healthy bundle (``example``) + one bundle whose
``build_server()`` pre-flight ``sys.exit(1)``s (``failing``) -> the container comes up bare-but-live
and serves the healthy bundle. License-free (``KX_MCP_AUTH=unset``, no pykx).

X.9 — live multi-backend composition: two real bundles (``example`` + ``example2``, the same fixture
code mounted twice under distinct namespaces) on one container -> no namespace collision, both
reachable, and the audit line attributes each dispatch to the right namespaced target. Every
existing live/integration spawn is single-bundle; this is the first to prove the instance-safety
contract (fresh server + fresh tool closures per ``build_server()`` call) holds with two real
backends together over the wire.
"""

from __future__ import annotations

import asyncio

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def test_failing_bundle_degrades_gracefully_over_the_wire(spawn_container):
    """example (healthy) + failing (pre-flight exits) -> bare-but-live serving only example."""
    # If the failing bundle took the container down, spawn_container's readiness wait would raise
    # here — so reaching past this line already proves "never crash the container" over the wire.
    url, _ = spawn_container("example,failing", KX_MCP_AUTH="unset")

    async def go() -> tuple[set[str], object]:
        async with Client(StreamableHttpTransport(url)) as client:
            names = {t.name for t in await client.list_tools()}
            result = await client.call_tool("example_echo", {"text": "hi"})
            return names, result.data

    names, echoed = asyncio.run(go())

    # The healthy bundle is fully live: its namespaced tool is registered and callable.
    assert "example_echo" in names
    assert echoed == "hi"
    # The failing bundle contributed nothing — no half-mounted namespace leaked through.
    assert not any(n.startswith("failing_") for n in names), names

    # Raw transport check: the streamable-http session layer answers rather than hanging. A
    # session-less POST is rejected at the transport layer before it reaches composition/dispatch
    # (missing session ID -> 400), so this isn't proof against a 500 from the failing bundle — that
    # proof is the three asserts above. This just confirms the server is live and responsive.
    resp = httpx.post(
        url,
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 400, (resp.status_code, resp.text)


def test_two_backends_compose_no_collision_over_the_wire(spawn_container):
    """example + example2 (same fixture code, two namespaces) -> no collision, both reachable, and
    the audit line attributes each dispatch to the right namespaced target."""
    url, proc = spawn_container("example,example2", KX_MCP_AUTH="unset", KX_MCP_LOG_LEVEL="INFO")

    async def go() -> tuple[set[str], object, object]:
        async with Client(StreamableHttpTransport(url)) as client:
            names = {t.name for t in await client.list_tools()}
            a = await client.call_tool("example_echo", {"text": "a"})
            b = await client.call_tool("example2_echo", {"text": "b"})
            return names, a.data, b.data

    names, echoed_a, echoed_b = asyncio.run(go())

    # No namespace collision: the identically-named bare tool from each bundle stays distinct.
    assert {"example_echo", "example2_echo"} <= names, names
    # Both independently reachable: each namespace answers its own call correctly, not the other's.
    assert (echoed_a, echoed_b) == ("a", "b")

    proc.terminate()
    try:
        logs, _ = proc.communicate(timeout=10)
    except Exception:
        proc.kill()
        logs, _ = proc.communicate()
    logs = logs or ""

    # Audit attributes each dispatch to the correct target prefix, not a shared/blurred one.
    assert "target=example_echo outcome=ok" in logs, logs
    assert "target=example2_echo outcome=ok" in logs, logs
