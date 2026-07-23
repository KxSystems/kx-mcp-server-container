"""Zero-config STDIO bundling smoke: spawn the container and reach a tool over the wire.

Two layers, both on the dependency-free `example` fixture (one `echo` tool) so this runs with no
PyKX license and no live KDB-X — the real kdbx/kdbai backends can't be used here because their
`build_server()` does an eager connectivity pre-flight and `sys.exit(1)`s without a reachable
database.

- `test_stdio_smoke_in_process` — the deterministic, always-on proof: an in-memory FastMCP client
  over the *composed parent* (`build_app(["example"])`), exercising composition + `example_*`
  namespacing + a tool round-trip.
- `test_stdio_smoke_subprocess` — the real-transport proof: spawn `kx_mcp_core.launcher` as a child
  over **actual STDIO** and drive it with a stdio client. Bundle selection stays explicit
  (`--bundles example`); the fixture is put on the child's PYTHONPATH because it is a test
  fixture, not an installed dist.
"""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from kx_mcp_core.launcher import build_app

_FIXTURE_SRC = str(Path(__file__).parent.parent.parent / "fixtures" / "kx-mcp-example" / "src")


def test_stdio_smoke_in_process():
    """Composed parent + an in-memory client reach the namespaced tool — no backend, no license."""

    async def go() -> str:
        async with Client(build_app(["example"], name="smoke")) as client:
            names = {t.name for t in await client.list_tools()}
            assert "example_echo" in names
            result = await client.call_tool("example_echo", {"text": "hello"})
            return result.data

    assert asyncio.run(go()) == "hello"


def test_stdio_smoke_subprocess():
    """Spawn the launcher as a STDIO child and reach a tool over the real transport."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([_FIXTURE_SRC, os.environ.get("PYTHONPATH", "")])}
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "kx_mcp_core.launcher", "--bundles", "example", "--transport", "stdio"],
        env=env,
    )

    async def go() -> str:
        async with Client(transport) as client:
            names = {t.name for t in await client.list_tools()}
            assert "example_echo" in names
            result = await client.call_tool("example_echo", {"text": "over-stdio"})
            return result.data

    assert asyncio.run(go()) == "over-stdio"
