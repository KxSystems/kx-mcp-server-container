"""The container surfaces its own audit line when run via the launcher — no external logging glue.

Regression for the gap that forced a `basicConfig` wrapper: the audit middleware logs on
`kx_mcp.audit` at INFO, but the launcher configured no handler, so the line was silently dropped
(`server.py`'s `basicConfig` masked it for the glue path only). `configure_logging` — wired into the
launcher with a `KX_MCP_LOG_LEVEL` knob — fixes it. Spawns the launcher as a real child via the
`spawn_container` fixture (tests/conftest.py) and reads its merged stdout/stderr; license-free
`example` bundle. Logging wiring is exactly the kind of bug in-process tests miss.
"""

from __future__ import annotations

import asyncio
import subprocess

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"

# keypair / mint — injected from the repo-root conftest.py


def _invoke_tool_and_capture_output(spawn_container, tool="example_echo", args=None, auth=None, **env: str) -> str:
    """Spawn the launcher, invoke a tool (optionally bearing a token), return the process output."""
    url, proc = spawn_container(KX_MCP_AUTH=env.pop("KX_MCP_AUTH", ""), **env)

    async def go() -> None:
        async with Client(StreamableHttpTransport(url, auth=auth)) as client:
            await client.call_tool(tool, args if args is not None else {"text": "hello"})

    asyncio.run(go())
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out


def _parse_audit_line(out: str) -> dict:
    """Pull the single ``audit ...`` line out of captured process output and parse it into a
    key=value dict — mirrors the parser in tests/deterministic/unit/test_audit_shape.py.

    The formatted line is prefixed with a timestamp/level/logger name (e.g.
    ``2026-07-07 ... INFO kx_mcp.audit audit subject=...``), so find the ``audit `` marker inside
    the line rather than requiring it at position 0."""
    marker = "audit subject="
    matches = [line[line.index(marker):] for line in out.splitlines() if marker in line]
    assert len(matches) == 1, matches
    fields: dict[str, str] = {}
    for token in matches[0][len("audit "):].split():
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


def test_launcher_surfaces_audit_line_by_default(spawn_container):
    """No logging glue: a tool call through the launcher emits the audit line to the process output."""
    out = _invoke_tool_and_capture_output(spawn_container)
    assert "audit subject=anonymous action=tool_invoke target=example_echo outcome=ok" in out


def test_log_level_suppresses_audit_line(spawn_container):
    """KX_MCP_LOG_LEVEL=WARNING raises the bar above the INFO audit line — the knob works."""
    out = _invoke_tool_and_capture_output(spawn_container, KX_MCP_LOG_LEVEL="WARNING")
    assert "audit subject=" not in out


def test_audit_line_carries_authenticated_subject_over_the_wire(tmp_path, keypair, mint, spawn_container):
    """The full who/what/target/outcome shape holds over a real subprocess + HTTP transport, with
    subject populated from a real validated bearer (not just 'anonymous', per the in-process proof
    in test_audit_shape.py). Complements test_authz_integration.py's over-the-wire proof of the
    decision/adapter fields."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    out = _invoke_tool_and_capture_output(
        spawn_container,
        tool="example_whoami",
        args={},
        auth=mint(client_id="alice"),
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    fields = _parse_audit_line(out)
    assert fields["subject"] == "alice"
    assert fields["action"] == "tool_invoke"
    assert fields["target"] == "example_whoami"
    assert fields["outcome"] == "ok"


def test_denied_bearer_emits_authenticate_audit_over_the_wire(tmp_path, keypair, mint, spawn_container):
    """A rejected bearer is an access decision the dispatch audit can never record — the 401 ends
    the request before dispatch — so the verifier records it. Over a real subprocess + HTTP so the
    whole chain (SDK bearer backend → wrapped verify_token → kx_mcp.audit → configure_logging) is
    exercised, not just the wrapper."""
    import httpx

    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)
    url, proc = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    response = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {mint(exp_delta=-10, client_id='svc-1')}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        timeout=10,
    )
    assert response.status_code == 401

    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    fields = _parse_audit_line(out)  # exactly one audit line: the denial, no dispatch happened
    assert fields["action"] == "authenticate"
    assert fields["target"] == "static"
    assert fields["outcome"] == "denied"
    assert fields["error"] == "invalid_token"
    assert fields["reason"] == "expired"
    assert fields["claimed_sub"] == "svc-1"
