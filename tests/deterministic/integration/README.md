# Integration tests

**Real subprocess over the wire, deterministic, license-free.** Spawns the MCP container as
a child process and drives it via a real HTTP or STDIO client. Marked `@pytest.mark.integration`
and runs in CI by default.

> **Run all commands from the repo root** (`kx-mcp-server-container/`), not from this directory.

These tests catch bugs that unit tests cannot: auth wiring through the FastMCP HTTP transport
stack, the `AccessToken` contextvar crossing the namespace mount boundary, and the JWKS fetch
path. JWT verification happens at the transport layer — in-process clients bypass it entirely,
which is why these tests exist as a separate tier.

## What is tested here

| File | What it covers |
|---|---|
| `test_audit_logging.py` | The launcher surfaces its own `kx_mcp.audit` line (`configure_logging` + `KX_MCP_LOG_LEVEL`) — spawns the real launcher and reads its output; regression for the silently-dropped audit line. |
| `test_auth_integration.py` | Over-the-wire auth: valid bearer → tool reachable; authenticated principal visible in mounted tool; no bearer → 401; expired / wrong-aud / missing-scope → 401; JWKS mode end-to-end; `KX_MCP_AUTH=unset` zero-config posture. |
| `test_authz_integration.py` | The capability check over the wire (`KX_MCP_AUTH=static` + `KX_MCP_AUTHZ=static` + a YAML policy): an `@authorize` deny inside a *mounted* tool surfaces as a clean tool error, and the decision is audited. |
| `test_composition_integration.py` | Container composition over the wire: a bundle whose `build_server()` `sys.exit(1)`s → the container comes up bare-but-live serving the healthy bundle; two bundles mounted at distinct namespaces → no collision, both reachable, audit attributes each dispatch to the right target. |
| `test_kx_auth_assertion_gate.py` | q-level regression for the `kx.auth` assertion gate (runs `kx_auth_assertion_gate.q` in a real `q` process): bind default-denies until configured, keys on the caller, password modes behave. The tier's one exception to license-free — **self-skips** when no `q` binary/license is present. |
| `test_outbound_integration.py` | Outbound wiring under composition: `current_principal()` and `exchange()` fire inside a mounted bundle on a real dispatched call — inbound bearer → principal crosses the mount boundary → outbound credential. |
| `test_stdio_smoke.py` | STDIO bundling: in-process composed parent + `example_echo` tool round-trip; real STDIO subprocess driven via `StdioTransport`. |

## Prerequisites

- No PyKX license — uses the `kx_mcp_example` fixture bundle (license-free).
- No live backend — the `example` bundle has no DB pre-flight.
- The `jwks_uri` fixture (repo-root `conftest.py`) starts an in-process JWKS HTTP server on
  `127.0.0.1` — no external IdP needed.

## How to run

```bash
# Just this tier
just test-integration
# or: uv run pytest -m integration

# This directory only
uv run pytest tests/deterministic/integration/ -v

# A single test
uv run pytest tests/deterministic/integration/test_auth_integration.py -k test_authenticated_principal_visible_in_mounted_tool
```

## Why subprocess and not threads?

JWT verification happens at FastMCP's HTTP transport layer. In-process clients skip that layer —
the `AccessToken` contextvar is never populated. A subprocess + real HTTP is the only way to prove
the auth wiring (and the principal crossing the mount boundary) actually works end-to-end. This is
the lesson carried from `aimeta`: mocked tests passed while a real transport bug went undetected.

## What's NOT tested here

- Verifier-level logic (issuer / audience / scope / expiry / kid) — those are covered more
  exhaustively in `../unit/test_auth.py` without the subprocess overhead.
- Real IdP token acquisition and live JWKS validation (see `../realidp/`).
