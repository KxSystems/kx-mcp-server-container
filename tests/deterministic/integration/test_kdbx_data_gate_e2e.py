"""PEP-2 data-gate end-to-end: `KDBX_DB_DATA_GATE=true` through a real q host + the real container.

Everything up to this file proves the mechanism in isolation — the q module's `entitled[]` against
real q (``kx_auth_assertion_gate.q``), and the Python adapter's shape with q mocked out
(``test_authz_kx_entitlements.py``). Neither proves the WIRE agrees: the Python side sends
``kx.SymbolAtom``/``kx.SymbolVector`` and expects a specific return shape back from
``.kx.auth.entitled``; a marshalling mismatch would fail closed at a customer's first query with
every one of those tests green. This file drives the real, mounted `kdbx` bundle over a real MCP
client call against a real q process, with `KDBX_DB_ASSERT_IDENTITY=true` + `KDBX_DB_DATA_GATE=true`,
so the SQL tool and the `tables://all` metadata resource exercise their actual obligation-handling
against real `entitled` verdicts — not a mock.

No live IdP needed: `KX_MCP_AUTH=static` + the root ``conftest.py``'s `keypair`/`mint` fixtures mint
locally-signed RS256 tokens with whatever `sub`/`groups` claims a persona needs, so this stays in the
deterministic integration tier, not realidp.

**Self-skipping**: needs a `q` binary and a kdb-x license, same convention as
``test_kx_auth_assertion_gate.py`` / ``test_kx_auth_rebind.py`` (and the same host-spawn shape as the
kdbx realidp ferry lane's `kdbx_ferry_host` fixture, trimmed of its live-Keycloak dependency — this
host wires the connection-level gate through `kx.auth`'s own `configure[]`/`activate[]` rather than
q's built-in `-u`/`-U`, and does NOT wrap `.s.e` — that's the OTHER, q-side-SQL-gate pattern the ferry
host demonstrates; this one exercises the container-side explicit-consult mechanism instead).
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

_HOST_Q = Path(__file__).with_name("kdbx_data_gate_host.q")
_SVC_USER = "kxmcp"
_SVC_PASSWORD = "kdbx-data-gate-test-svc-pw"


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
def data_gate_host():
    """Spawn a real `q` process loaded with kdbx_data_gate_host.q for the whole module.

    Yields (host, port, svc_user, svc_password). Skips cleanly if no `q` binary is found or the
    process fails to license-start; fails loudly if it starts but never becomes ready.

    Readiness is polled via a real qIPC call checking `.kx.auth.bind` is defined — mirroring the
    container's own pre-flight, and the kdbx realidp ferry lane's `kdbx_ferry_host` fixture — rather
    than a bare socket check, since q opens its listening port before the script body (which loads
    kx.auth) finishes running.
    """
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary — the kdbx data-gate e2e regression needs a kdb-x install")

    env = dict(os.environ)
    # Same QHOME/QPATH override as test_kx_auth_assertion_gate.py / kdbx_ferry_host: a co-located
    # kdb-x install resolves both the license (QHOME -> kc.lic) and the module path (QHOME/mod,
    # where `just install-modules` symlinks kx.auth) — set explicitly rather than relying on
    # ambient/pykx-mutated env (pykx's own import overrides QHOME for its bundled q lib dir).
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
            pytest.skip(f"no kdb-x license available — skipping data-gate e2e ({out.strip()[:120]})")
        if out:
            pytest.fail(f"data_gate_host exited early ({proc.returncode}):\n{out}")
        pytest.fail(f"data_gate_host never became ready on :{port}: {last_exc}")

    yield "127.0.0.1", port, _SVC_USER, _SVC_PASSWORD

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _spawn_kdbx_container(spawn_container, data_gate_host, keypair, *, data_gate: bool):
    """Spawn the real container mounting `kdbx`, pointed at the shared q host."""
    host, port, svc_user, svc_password = data_gate_host
    _, pub = keypair
    pub_path = Path(f"/tmp/kdbx-data-gate-pub-{os.getpid()}.pem")
    pub_path.write_text(pub)
    # `data_gate_host`'s readiness probe already imported pykx in THIS pytest process, poisoning
    # os.environ; spawn_container's child inherits it as its base, not an override we control — so
    # strip it, scoped to just this call (see tests/_pykx_env.py).
    with stripped_from_os_environ():
        return spawn_container(
            "kdbx",
            KX_MCP_AUTH="static",
            KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
            KX_MCP_AUTH_ISSUER=ISSUER,
            KX_MCP_AUTH_AUDIENCE=AUDIENCE,
            KDBX_DB_HOST=host,
            KDBX_DB_PORT=str(port),
            # KDBConfig.timeout defaults to 1 second (settings.py) — tight for a subprocess racing
            # its own PyKX/FastMCP startup; generous rather than flaky (not the fix for the issue
            # above, just cheap robustness on top of it).
            KDBX_DB_TIMEOUT="10",
            KDBX_DB_USERNAME=svc_user,
            KDBX_DB_PASSWORD=svc_password,
            KDBX_DB_ASSERT_IDENTITY="true",
            KDBX_DB_DATA_GATE="true" if data_gate else "false",
        )


def _call(url: str, token: str, tool: str, args: dict):
    """Call a tool over the wire and return its structured payload.

    ``raise_on_error=False`` because a failed dispatch now carries ``isError: true`` (the tool-result
    contract — ``docs/extending.md`` § Signalling failure) and fastmcp's Client raises on that by
    default. These tests are *about* the denial payloads, so they need the result, not an exception.

    The flag is asserted here rather than in each test: every failure payload this file produces
    must also be flagged at the protocol level, so routing all four personas through one check makes
    this file the over-the-wire proof of the contract for the whole data-gate path.
    """

    async def go():
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            return await client.call_tool(tool, args, raise_on_error=False)

    outcome = asyncio.run(go())
    # `.data` is fastmcp's deserialized convenience accessor and it is NOT populated on an
    # error-flagged result — `.structured_content` is. So read that first, or every assertion below
    # would run against None the moment a payload is correctly flagged as a failure.
    result = outcome.structured_content if outcome.is_error else outcome.data
    _skip_on_embedded_license_contention(result)
    if isinstance(result, dict) and result.get("status") in ("error", "permission_denied"):
        assert outcome.is_error is True, (
            f"{tool} returned {result.get('status')!r} without isError: true — "
            "a failure that reads as a success to the host and to the metrics counter"
        )
    else:
        assert outcome.is_error is False
    return result


def _skip_on_embedded_license_contention(result) -> None:
    """Skip (don't fail) when the SQL tool's local `kx.CharVector(...)` construction can't check
    out an embedded-pykx license slot — separate from the plain qIPC connection this file otherwise
    exercises. Reproducibly hit only when the full suite runs: the parent pytest process has already
    bootstrapped an embedded pykx engine (via the ~190 kdbx unit tests that `import pykx` to patch
    `pykx.SyncQConnection`), plus this file's own q host and the container's own embedded engine — a
    third concurrent q process, which a single-seat dev license doesn't have a slot for. This file's
    other assertions (mount succeeds, connectivity/pre-flight, denial/scope-down paths that never
    reach `kx.CharVector`) are unaffected and still fail loudly on a real regression — only the
    specific "no license slot" message is treated as an environment limit, matching the self-skip
    convention the rest of this q-level tier already uses for "no license available".
    """
    if isinstance(result, dict) and "license" in str(result.get("message", "")).lower():
        pytest.skip(f"embedded-pykx license contention under the full suite — {result['message']}")


def _read(url: str, token: str, uri: str) -> str:
    async def go():
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            result = await client.read_resource(uri)
            return result[0].text

    return asyncio.run(go())


# --- 1. all-entitled: normal success, and the pre-flight (entitled_available) implicitly proven ----


def test_all_entitled_persona_queries_trades_and_lists_it(data_gate_host, keypair, mint, spawn_container):
    url, _ = _spawn_kdbx_container(spawn_container, data_gate_host, keypair, data_gate=True)
    token = mint(client_id="alice", extra_claims={"groups": ["trader"]})

    result = _call(url, token, "kdbx_run_sql_query", {"query": "SELECT * FROM trades"})
    assert result["status"] == "success", result
    assert result["data"], "expected real trades rows back"

    listing = _read(url, token, "tables://kdbx/all")
    assert '"trades"' in listing
    assert "permission_denied" not in listing


# --- 2. none-entitled: clean denial, query never reaches the data --------------------------------


def test_none_entitled_persona_denied_on_secrets(data_gate_host, keypair, mint, spawn_container):
    url, _ = _spawn_kdbx_container(spawn_container, data_gate_host, keypair, data_gate=True)
    token = mint(client_id="bob")  # no `trader` group -> entitled to nothing

    result = _call(url, token, "kdbx_run_sql_query", {"query": "SELECT * FROM secrets"})
    assert result["status"] == "error"
    assert result["error_type"] == "permission_denied"
    # A HARD deny (none entitled) carries no obligations at all — denied_tables/entitled_tables are
    # scope-down-only fields (see test 3). The refused table is named in the message/reason instead.
    assert "denied_tables" not in result
    assert "secrets" in result["message"]


def test_none_entitled_persona_denied_on_secrets_however_it_is_cased(
    data_gate_host, keypair, mint, spawn_container
):
    """REGRESSION (CRITICAL — see mcp-container/adversarial-review-2026-08.md).

    The same denial as the test above, with the table name RE-CASED. `derive_tables` matched
    case-sensitively while the SQL interface resolves identifiers case-insensitively, so `SECRETS`
    derived no tables at all, the tool concluded "no tables referenced" and skipped
    `consult_data_gate` entirely, and `.s.e` then resolved `secrets` and returned it — bob, entitled
    to nothing, received the full confidential table with `status: success`.

    This host deliberately does NOT wrap `.s.e` (that is the other, ferry-host pattern), which is
    what makes it the right shape to pin: in this deployment the container-side consult is the only
    gate, so a false negative in the derivation fails OPEN with nothing behind it. Every pre-existing
    case in this file used the table name in its stored case, which is why the gap survived.
    """
    url, _ = _spawn_kdbx_container(spawn_container, data_gate_host, keypair, data_gate=True)
    token = mint(client_id="bob")  # no `trader` group -> entitled to nothing

    for query in ("SELECT * FROM SECRETS", "select * from Secrets", "SELECT * FROM sEcReTs"):
        result = _call(url, token, "kdbx_run_sql_query", {"query": query})
        assert result["status"] == "error", (query, result)
        assert result["error_type"] == "permission_denied", (query, result)
        assert "secrets" in result["message"], (query, result)


# --- 3. partial-entitled: scope-down guidance on the SQL tool, filtered listing on the resource ---


def test_partial_entitled_persona_gets_scope_down_guidance(
    data_gate_host, keypair, mint, spawn_container
):
    url, _ = _spawn_kdbx_container(spawn_container, data_gate_host, keypair, data_gate=True)
    token = mint(client_id="carol", extra_claims={"groups": ["trader"]})

    result = _call(
        url, token, "kdbx_run_sql_query",
        {"query": "SELECT * FROM trades, secrets"},
    )
    assert result["status"] == "error"
    assert result["error_type"] == "permission_denied"
    assert result["entitled_tables"] == ["trades"]
    assert result["denied_tables"] == ["secrets"]
    assert "trades" in result["message"]  # the re-scope guidance names the entitled subset

    listing = _read(url, token, "tables://kdbx/all")
    assert '"trades"' in listing
    assert '"secrets"' not in listing
    assert '"hiddenByEntitlements": 1' in listing


# --- 4. the gate is opt-in: DATA_GATE=false is the byte-identical legacy path ---------------------


def test_data_gate_off_is_the_legacy_path(data_gate_host, keypair, mint, spawn_container):
    url, _ = _spawn_kdbx_container(spawn_container, data_gate_host, keypair, data_gate=False)
    token = mint(client_id="dave")  # entitled to NOTHING under the gate -- irrelevant when it's off

    result = _call(url, token, "kdbx_run_sql_query", {"query": "SELECT * FROM secrets"})
    assert result["status"] == "success", (result, "gate off")

    listing = _read(url, token, "tables://kdbx/all")
    assert '"secrets"' in listing
    # hiddenByEntitlements is unconditionally present (metadata_model.py always sets it) — with the
    # gate off, _entitled_names short-circuits to hidden=0 rather than the field being absent.
    assert '"hiddenByEntitlements": 0' in listing
