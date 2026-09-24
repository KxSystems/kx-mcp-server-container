"""A non-string `sub` claim: binds silently, then breaks the first policy check with a raw q
`'type` error instead of a clean `permission_denied` — through a real q host + the real container.

`_bind_principal`/`project_principal` never type-check `subject`/`claims["sub"]`. A JWT with a
`sub` claim that's a JSON NUMBER (not a string) ferries through `.kx.auth.bind` without complaint —
the mismatch only bites the first time a policy compares that value against a symbol, which q
resolves as a `'type` error, not the module's own `'denied:` convention. `denial.py::is_denial`
correctly does NOT mistake that for a clean denial, so the container's own `except Exception`
catches it and falls through to a bare `{"status": "error", "message": str(e)}` — no `error_type`
at all, giving the agent a confusing infrastructure-shaped error instead of a structured
`permission_denied`. This needs the REAL frozen `kx.auth` q module (its `asSym`/`promote`
behavior is exactly what's under test) — not mockable meaningfully.

**Self-skipping**: needs a `q` binary and a kdb-x license, same convention as
``test_kdbx_data_gate_e2e.py``.
"""

from __future__ import annotations

import asyncio
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

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"

_HOST_Q = Path(__file__).with_name("kdbx_malformed_identity_host.q")
_SVC_USER = "kxmcp"
_SVC_PASSWORD = "kdbx-malformed-identity-test-svc-pw"


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
def malformed_identity_host():
    """Spawn a real `q` process loaded with kdbx_malformed_identity_host.q for the whole module.

    Yields (host, port, svc_user, svc_password). Skips cleanly if no `q` binary is found or the
    process fails to license-start; fails loudly if it starts but never becomes ready. Same shape
    as `test_kdbx_data_gate_e2e.py`'s `data_gate_host` fixture.
    """
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary — the malformed-identity e2e regression needs a kdb-x install")

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
            probe = kx_mod.SyncQConnection(
                host="127.0.0.1", port=port, username=_SVC_USER, password=_SVC_PASSWORD, timeout=2,
            )
            try:
                bind_available = probe("@[{.kx.auth.bind;1b};(::);{0b}]").py()
            finally:
                probe.close()
            if bind_available:
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
            pytest.skip(f"no kdb-x license available — skipping malformed-identity e2e ({out.strip()[:120]})")
        if out:
            pytest.fail(f"malformed_identity_host exited early ({proc.returncode}):\n{out}")
        pytest.fail(f"malformed_identity_host never became ready on :{port}: {last_exc}")

    yield "127.0.0.1", port, _SVC_USER, _SVC_PASSWORD

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _call(url: str, token: str, tool: str, args: dict):
    """Call a tool over the wire and return its structured payload.

    ``raise_on_error=False`` and ``.structured_content``, both required since the tool-result
    contract landed: a failed dispatch now carries ``isError: true``, fastmcp's Client raises on
    that by default, and ``.data`` is not populated on an error-flagged result. This test is
    *about* a failure payload, so it needs the result, not an exception. Same pattern as
    ``test_kdbx_data_gate_e2e.py`` — see ``docs/extending.md`` § Signalling failure.
    """

    async def go():
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            return await client.call_tool(tool, args, raise_on_error=False)

    outcome = asyncio.run(go())
    result = outcome.structured_content if outcome.is_error else outcome.data
    if isinstance(result, dict) and "license" in str(result.get("message", "")).lower():
        pytest.skip(f"embedded-pykx license contention under the full suite — {result['message']}")
    return result


def test_non_string_sub_claim_denies_cleanly_not_a_raw_type_error(
    malformed_identity_host, keypair, mint, spawn_container
):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb-x backend).

    A `sub` claim that's a JSON number (not a string) binds without complaint, then breaks the
    first policy check with a raw q 'type error instead of a clean permission_denied. The tool
    must surface a structured denial, not an infra-shaped error.

    KDBX_DB_DATA_GATE is deliberately left OFF here: that path routes through decide()'s own
    adapter-exception fail-closed wrapper, which already converts any raised exception into a
    clean deny — accidentally masking this exact bug. The host's `.s.e` wrap (the "ferry host"
    pattern) is what actually exercises the unguarded path this bug lives on.
    """
    host, port, svc_user, svc_password = malformed_identity_host
    _, pub = keypair
    pub_path = Path(f"/tmp/kdbx-malformed-sub-pub-{os.getpid()}.pem")
    pub_path.write_text(pub)
    token = mint(client_id="alice", extra_claims={"sub": 12345})

    with stripped_from_os_environ():
        url, _ = spawn_container(
            "kdbx",
            KX_MCP_AUTH="static",
            KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
            KX_MCP_AUTH_ISSUER=ISSUER,
            KX_MCP_AUTH_AUDIENCE=AUDIENCE,
            KDBX_DB_HOST=host,
            KDBX_DB_PORT=str(port),
            KDBX_DB_TIMEOUT="10",
            KDBX_DB_USERNAME=svc_user,
            KDBX_DB_PASSWORD=svc_password,
            KDBX_DB_ASSERT_IDENTITY="true",
        )

    result = _call(url, token, "kdbx_run_sql_query", {"query": "SELECT * FROM trades"})
    assert result["status"] == "error", result
    assert result.get("error_type") == "permission_denied", result
