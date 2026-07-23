#!/usr/bin/env python3
"""Prove the wheels-assembled consumer server is healthy: launch it, complete an MCP handshake.

Spawns server.py (the consumer's Acme-only assembly), waits for it to bind, then connects an MCP
client over streamable-http and performs the `initialize` handshake + `list_tools`. A successful
handshake is the health signal; the mounted tools are reported for visibility.

No external backend is required. The Acme bundle's connection is an in-process stub, so its pre-flight
always succeeds and its `acme_*` tools always mount — a healthy handshake listing them is the expected
result. (A real backend whose pre-flight fails would be disabled by `try_mount_bundle` and the
container would still come up healthy but bare — the "never crash the container" invariant.)

Run:  uv run python check_health.py
Exit: 0 if the server answered the MCP handshake, 1 otherwise.
"""

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

HERE = Path(__file__).parent
HOST, PORT = "127.0.0.1", 8000  # must match server.py's app.run(...)
URL = f"http://{HOST}:{PORT}/mcp"


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((host, port)) != 0


def _wait_listening(host: str, port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early (rc={proc.returncode}) — see its log above")
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError("server did not start listening in time")


async def _handshake(url: str) -> list[str]:
    # No `auth=` — KX_MCP_AUTH is unset by default, so the server accepts the unauthenticated handshake.
    async with Client(StreamableHttpTransport(url)) as client:
        # The async-context enter performs the MCP initialize handshake; reaching here means it succeeded.
        tools = sorted(t.name for t in await client.list_tools())
        return tools


def main() -> int:
    if not _port_is_free(HOST, PORT):
        print(f"ERROR: {HOST}:{PORT} is already in use — free it (or stop the other server) and retry.",
              file=sys.stderr)
        return 1

    cmd = [sys.executable, str(HERE / "server.py")]
    print(f"[health] launching consumer server: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_listening(HOST, PORT, proc)
        print(f"[health] server listening on {URL} — performing MCP initialize handshake ...")
        tools = asyncio.run(_handshake(URL))
        print("[health] OK — MCP handshake succeeded; the server is healthy.")
        acme_tools = [t for t in tools if t.startswith("acme_")]
        if acme_tools:
            print(f"[health] acme bundle mounted: {len(acme_tools)} acme_* tool(s): {acme_tools}")
        else:
            print("[health] WARNING: no acme_* tools mounted — the acme bundle failed to register.")
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001 — a health check reports any failure as unhealthy
        print(f"[health] FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        print("\n--- server log ---")
        for line in (out or "").splitlines():
            if any(k in line for k in ("acme", "audit", "pre-flight", "Mounted", "WARNING", "ERROR", "Uvicorn", "Started")):
                print("   " + line)


if __name__ == "__main__":
    raise SystemExit(main())
