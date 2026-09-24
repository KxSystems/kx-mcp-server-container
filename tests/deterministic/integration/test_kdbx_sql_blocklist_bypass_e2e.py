"""SQL write-keyword blocklist bypass: a SELECT-prefixed, `;`-chained destructive statement through
a real q host + the real container.

`run_query_impl`'s dangerous-keyword guard (`kdbx_run_sql_query.py`) only checks
`keyword in query_upper and not query_upper.startswith('SELECT')` — so the ENTIRE guard is dead code
for any query that starts with `SELECT`, regardless of what follows. q's `.s.e` SQL interface allows
`;`-chained statements, so `SELECT * FROM trades; DROP TABLE trades` sails straight through unchallenged
and actually drops the table. This file proves it against a real q process, not a mock — a Python-side
string check can't be trusted to characterize what a real SQL dialect actually accepts.

No identity assertion, no kx.auth, no IdP needed: `KX_MCP_AUTH=unset` (the single-principal posture) is
enough, since this bug lives entirely in the SQL tool's own guard, independent of the auth/authz seam.

**Self-skipping**: needs a `q` binary and a kdb-x license, same convention as
``test_kdbx_data_gate_e2e.py``.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from _pykx_env import stripped_from_os_environ

pytestmark = pytest.mark.integration

_HOST_Q = Path(__file__).with_name("kdbx_sql_blocklist_host.q")


def _q_binary() -> str | None:
    """The ``q`` on PATH, else a co-located kdb-x install under ~/.kx (the repo's dev convention)."""
    found = shutil.which("q")
    if found:
        return found
    fallback = Path.home() / ".kx" / "bin" / "q"
    return str(fallback) if fallback.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sql_host():
    """Spawn a real, plain `q` process (no auth, no kx.auth) loaded with
    kdbx_sql_blocklist_host.q for the whole module. Yields (host, port).

    Skips cleanly if no `q` binary is found or the process fails to license-start; fails loudly if
    it starts but never becomes ready. Readiness is polled via a real qIPC call checking `trades` is
    a live table — mirroring `test_kdbx_data_gate_e2e.py`'s `data_gate_host` fixture, trimmed of its
    identity-assertion machinery (this host has no `.z.pw`, so no username/password is needed).
    """
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary — the SQL-blocklist-bypass e2e regression needs a kdb-x install")

    env = dict(os.environ)
    kx = Path.home() / ".kx"
    if kx.exists():
        env["QHOME"] = str(kx)
        env["QPATH"] = str(kx / "mod")

    port = _free_port()
    proc = subprocess.Popen(
        [q, str(_HOST_Q), "-p", str(port)],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    import pykx as kx_mod  # local import: only this module needs a real (licensed) pykx

    deadline = time.time() + 20.0
    ready = False
    last_exc: Exception | None = None
    out = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            break
        try:
            probe = kx_mod.SyncQConnection(host="127.0.0.1", port=port, timeout=2)
            try:
                trades_live = probe("`trades in tables[]").py()
            finally:
                probe.close()
            if trades_live:
                ready = True
                break
        except Exception as exc:  # noqa: BLE001 — q not accepting connections yet, keep polling
            last_exc = exc
            time.sleep(0.25)

    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if "license" in out.lower() and ("error" in out.lower() or "no license" in out.lower()):
            pytest.skip(f"no kdb-x license available — skipping SQL-blocklist e2e ({out.strip()[:120]})")
        if out:
            pytest.fail(f"sql_host exited early ({proc.returncode}):\n{out}")
        pytest.fail(f"sql_host never became ready on :{port}: {last_exc}")

    yield "127.0.0.1", port

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _call(url: str, tool: str, args: dict):
    """Call a tool over the wire and return its structured payload.

    ``raise_on_error=False`` and ``.structured_content``, both required since the tool-result
    contract landed: a failed dispatch now carries ``isError: true``, fastmcp's Client raises on
    that by default, and ``.data`` is not populated on an error-flagged result. This test is
    *about* a failure payload, so it needs the result, not an exception. Same pattern as
    ``test_kdbx_data_gate_e2e.py`` — see ``docs/extending.md`` § Signalling failure.
    """
    import asyncio

    async def go():
        async with Client(StreamableHttpTransport(url)) as client:
            return await client.call_tool(tool, args, raise_on_error=False)

    outcome = asyncio.run(go())
    result = outcome.structured_content if outcome.is_error else outcome.data
    if isinstance(result, dict) and "license" in str(result.get("message", "")).lower():
        pytest.skip(f"embedded-pykx license contention under the full suite — {result['message']}")
    return result


def test_select_prefixed_chained_drop_is_rejected(sql_host, spawn_container):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb-x backend).

    The dangerous-keyword guard never fires once a query starts with SELECT, and q's `.s.e`
    permits `;`-chained statements — so a SELECT-prefixed DROP sails through today. This must be
    rejected, and the table must survive."""
    host, port = sql_host
    with stripped_from_os_environ():
        url, _ = spawn_container(
            "kdbx",
            KX_MCP_AUTH="unset",
            KDBX_DB_HOST=host,
            KDBX_DB_PORT=str(port),
            KDBX_DB_TIMEOUT="10",
        )

    result = _call(url, "kdbx_run_sql_query", {"query": "SELECT * FROM trades; DROP TABLE trades"})
    assert result["status"] == "error", result

    still_there = _call(url, "kdbx_run_sql_query", {"query": "SELECT * FROM trades"})
    assert still_there["status"] == "success", still_there
    assert still_there["data"], "trades must still exist"
