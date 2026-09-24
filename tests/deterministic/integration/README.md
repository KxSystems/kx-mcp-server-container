# Integration tests

**Real subprocess over the wire, deterministic, license-free.** Spawns the MCP container as
a child process and drives it via a real HTTP or STDIO client. Marked `@pytest.mark.integration`
and runs in CI by default.

> **Run all commands from the repo root** (`kx-mcp-server-container/`), not from this directory.

These tests catch bugs that unit tests cannot: auth wiring through the FastMCP HTTP transport
stack, the `AccessToken` contextvar crossing the namespace mount boundary, and the JWKS fetch
path. JWT verification happens at the transport layer — in-process clients bypass it entirely,
which is why these tests exist as a separate tier. The same argument applies to observability: a
`/metrics` scrape against an in-process ASGI transport (the unit tier) cannot see a real launcher
subprocess mis-wiring the route, or a real backend under-reporting `isError` — both proven only by
`test_observability_metrics_e2e.py`.

## What is tested here

| File | What it covers |
|---|---|
| `test_audit_logging.py` | The launcher surfaces its own `kx_mcp.audit` line (`configure_logging` + `KX_MCP_LOG_LEVEL`) — spawns the real launcher and reads its output; regression for the silently-dropped audit line. |
| `test_auth_integration.py` | Over-the-wire auth: valid bearer → tool reachable; authenticated principal visible in mounted tool; no bearer → 401; expired / wrong-aud / missing-scope → 401; JWKS mode end-to-end; `oidc_proxy` mode fronting the login (RFC 8414 metadata names the container, local DCR, startup discovery); `KX_MCP_AUTH=unset` zero-config posture. |
| `test_authz_integration.py` | The capability check over the wire (`KX_MCP_AUTH=static` + `KX_MCP_AUTHZ=static` + a YAML policy): an `@authorize` deny inside a *mounted* tool surfaces as a clean tool error, and the decision is audited. |
| `test_composition_integration.py` | Container composition over the wire: a bundle whose `build_server()` `sys.exit(1)`s → the container comes up bare-but-live serving the healthy bundle; two bundles mounted at distinct namespaces → no collision, both reachable, audit attributes each dispatch to the right target. |
| `test_kx_auth_assertion_gate.py` | q-level regression for the `kx.auth` module's assertion gate and the rest of its export surface (runs `kx_auth_assertion_gate.q` in a real `q` process): `bind` default-denies until a policy grants `assert`/`kx.identity`, keyed on the caller; `authorize`'s data S/A/R path; `entitled[]` (the PEP-2 scope-down verb — full/partial/none/empty-input, and its own default-deny gate); `configure[]`'s malformed-arg guard and delegate-before/enforce-after `pwCheck`; the HTTP path (`fromJson`, `serveHttp`'s per-request binding + case-insensitive header match + always-clears-on-error, `activateHttp`'s composition with a prior handler). One of the tier's two exceptions to license-free — **self-skips** when no `q` binary/license is present. |
| `test_kx_auth_rebind.py` | q-level regression for `.kx.auth.bind`'s wholesale-replacement semantics (runs `kx_auth_rebind.q` in a real `q` process): a re-bind with a narrower principal must fully replace the prior one, not merge column-wise (the store's value list must stay a general list); a stale field must not survive into a policy decision; an all-atoms principal must promote without signalling. Self-skips like the row above. |
| `test_kdbx_data_gate_e2e.py` | Full end-to-end for the PEP-2 data gate: spawns a real `q` host (`kdbx_data_gate_host.q`, two tables + a `kx.auth` policy) and the real container (`KX_MCP_AUTH=static`, `KDBX_DB_ASSERT_IDENTITY=true`, `KDBX_DB_DATA_GATE=true`), then drives the real `kdbx_run_sql_query` tool and `tables://kdbx/all` metadata resource over a real MCP client for four personas (all-entitled, none, partial/scope-down, gate-off). Proves the Python↔q wire for `entitled[]` agrees end to end — the risk mocked tests can't catch. Self-skips like the rows above; also self-skips its two success-path cases under embedded-pykx license-seat contention specific to a single-seat dev license running the full suite (never a failure — see the file's own docstring). |
| `test_observability_metrics_e2e.py` | The Prometheus metrics seam over the wire: `/metrics` 404s with `KX_MCP_METRICS` unset (license-free, `example` bundle) and 200s with it on; a real `kdbx_run_sql_query` success/failure pair is counted `outcome="ok"`/`outcome="error"` in `kx_mcp_dispatches_total` — the regression for the `isError` contract, which was originally discovered by running this seam against a live container while mocked tests still agreed with the bug (see `mcp-container/BACKLOG.md`); the in-flight gauge returns to 0 on the failure path; the kdbx-specific `kdbx_qipc_calls_total` lands in the *container's* scrape through a real mount; reading `tables://kdbx/all` fans out into more qIPC calls than its single dispatch count. Self-skips like the rows above for its kdbx-backed tests; its `/metrics`-disabled test needs neither `q` nor a license. |
| `test_outbound_integration.py` | Outbound wiring under composition: `current_principal()` and `exchange()` fire inside a mounted bundle on a real dispatched call — inbound bearer → principal crosses the mount boundary → outbound credential. |
| `test_stdio_smoke.py` | STDIO bundling: in-process composed parent + `example_echo` tool round-trip; real STDIO subprocess driven via `StdioTransport`. |

## Prerequisites

- No PyKX license for most of this tier — uses the `kx_mcp_example` fixture bundle (license-free).
- No live backend for most of this tier — the `example` bundle has no DB pre-flight.
- The `jwks_uri` and `oidc_issuer` fixtures (repo-root `conftest.py`) start in-process HTTP servers
  on `127.0.0.1` serving a JWKS endpoint and an OIDC discovery document — no external IdP needed.
- **Exception:** `test_kx_auth_assertion_gate.py`, `test_kx_auth_rebind.py`,
  `test_kdbx_data_gate_e2e.py`, and most of `test_observability_metrics_e2e.py` need a real `q`
  binary + kdb-x license (a `q` on `PATH` or `~/.kx/bin/q`, per the repo's dev convention) — they
  self-skip cleanly, at no cost, when absent. `test_observability_metrics_e2e.py`'s
  metrics-disabled test is the one exception within that file — it uses the license-free `example`
  bundle, so it still runs with no `q` install.

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
