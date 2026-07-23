"""Outbound wiring under composition — rows that package-level tests cannot cover.

Spawns the launcher with --bundles example and drives the ``example_propagate`` tool over HTTP.
Each test proves that ``current_principal()`` and ``exchange()`` both fire correctly inside a
mounted bundle on a real dispatched tool call:

    inbound bearer → container validates → principal crosses mount boundary
                                         → propagate() reads principal.token
                                         → calls exchange() against mock_sts
                                         → returns JSON with both sides asserted

Rows covered:
  3.1b  passthrough wiring — bearer forwarded unchanged; STS never called
  3.2b  rfc_8693 wiring — inbound bearer becomes exchange subject; STS receives it
  3.3   audit chain under composition — exchange() emits its audit line inside a real dispatch

Fixtures ``keypair``, ``mint``, ``mock_sts`` come from the repo-root ``conftest.py``;
``spawn_container`` comes from ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"


def _pub_key_path(keypair, tmp_path):
    _, pub = keypair
    p = tmp_path / "public.pem"
    p.write_text(pub)
    return p


def _spawn_static(spawn_container, keypair, tmp_path):
    """Spawn a container with KX_MCP_AUTH=static pointing at the test keypair."""
    return spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(_pub_key_path(keypair, tmp_path)),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )


# ---------------------------------------------------------------------------
# 3.1b — passthrough: current_principal().token forwarded unchanged
# ---------------------------------------------------------------------------


def test_passthrough_wires_principal_through_tool(tmp_path, keypair, mint, spawn_container):
    """passthrough: bearer from current_principal().token forwarded unchanged (3.1b).

    Proves the mount-boundary crossing and passthrough strategy in one tool call.
    The STS is not involved — passthrough returns the subject token as-is.
    """
    url, _ = _spawn_static(spawn_container, keypair, tmp_path)
    bearer = mint()

    async def go() -> dict:
        async with Client(StreamableHttpTransport(url, auth=bearer)) as client:
            result = await client.call_tool("example_propagate", {
                "strategy": "passthrough",
                "audience": AUDIENCE,
            })
        return json.loads(result.data)

    envelope = asyncio.run(go())
    assert envelope["principal_sub"] == "alice"
    assert envelope["strategy"] == "passthrough"
    assert envelope["exchanged_token_aud"] == AUDIENCE  # forwarded unchanged


# ---------------------------------------------------------------------------
# 3.2b — rfc_8693: inbound bearer exchanged for audience-scoped token
# ---------------------------------------------------------------------------


def test_rfc_8693_wires_exchange_through_tool(tmp_path, keypair, mint, mock_sts, spawn_container):
    """rfc_8693: inbound bearer becomes the exchange subject; STS receives it (3.2b).

    Asserts both sides in one call:
    - principal_sub == "alice"  → current_principal() crossed the mount boundary
    - exchanged_token_aud == "kdbai"  → exchange() produced the right audience-scoped token
    - captured[0]["subject_token"] == bearer  → the STS received the inbound token as the subject
    """
    url, _ = _spawn_static(spawn_container, keypair, tmp_path)
    token_url, captured = mock_sts
    bearer = mint()

    async def go() -> dict:
        async with Client(StreamableHttpTransport(url, auth=bearer)) as client:
            result = await client.call_tool("example_propagate", {
                "strategy": "rfc_8693",
                "token_url": token_url,
                "audience": "kdbai",
                "client_id": "mcp-container",
                "client_secret": "s3cret",
            })
        return json.loads(result.data)

    envelope = asyncio.run(go())
    assert envelope["principal_sub"] == "alice"
    assert envelope["strategy"] == "rfc_8693"
    assert envelope["exchanged_token_aud"] == "kdbai"

    assert len(captured) == 1
    assert captured[0]["subject_token"] == bearer


# ---------------------------------------------------------------------------
# 3.3 — audit chain under composition
# ---------------------------------------------------------------------------


def test_exchange_audit_chain(tmp_path, keypair, mint, mock_sts, spawn_container):
    """Full audit chain fires under composition: exchange() logs subject/strategy/audience/outcome (3.3).

    After the tool call the process is terminated and its merged stdout is scanned for the
    exchange audit line, proving the chain fired inside a real mounted-tool dispatch — not just
    in the package-level seam tests.
    """
    url, proc = _spawn_static(spawn_container, keypair, tmp_path)
    token_url, _ = mock_sts
    bearer = mint()

    async def go() -> None:
        async with Client(StreamableHttpTransport(url, auth=bearer)) as client:
            await client.call_tool("example_propagate", {
                "strategy": "rfc_8693",
                "token_url": token_url,
                "audience": "kdbai",
                "client_id": "mcp-container",
                "client_secret": "s3cret",
            })

    asyncio.run(go())

    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    assert "strategy=rfc_8693" in out
    assert "subject=alice" in out
    assert "audience=kdbai" in out
    assert "outcome=ok" in out
