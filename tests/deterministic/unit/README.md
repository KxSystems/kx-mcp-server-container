# Unit tests

**In-process, deterministic, no subprocess.** The fastest feedback tier — runs in CI by default
with no external dependencies (no license, no running server, no IdP).

> **Run all commands from the repo root** (`kx-mcp-server-container/`), not from this directory.

## What is tested here

| File | What it covers |
|---|---|
| `test_audit_shape.py` | The audit dispatch line has one uniform shape across every backend: everything flows through the single parent-level `AuditMiddleware`, pinned by mounting tiny fake bundles (no licenses needed). |
| `test_auth.py` | The inbound-auth verifier seam: `build_auth_provider`, `verify_token`, `current_principal`, `AuditMiddleware`. Drives the verifier and tools via FastMCP's in-memory client. |
| `test_auth_cli_contract.py` | Cross-library drift guard: the same token run through `kx_auth_core.verify_token` (joserfc, used by `kx auth introspect`) and FastMCP's `JWTVerifier` must agree. Fails before the CLI silently disagrees with what the container enforces. |
| `test_authorize.py` | The capability check (PEP-1) in-process: the `@authorize` decorator over the static YAML capability adapter — route-only allow when authz is off, namespace derived from the resource prefix, group∩grant decisions, deny → `AuthorizationDenied`. |
| `test_composition.py` | Assembly + mount/namespace: bundle auto-registration, namespace prefix collision, `try_mount_bundle` graceful-degradation (bad build_server disables only that backend). |
| `test_discovery.py` | The shared `addins/` discovery helper (`register_components`): components register onto the passed-in instance, import failures are fail-fast, `*.py.template` files are inert, and two sibling `addins/` packages don't collide in `sys.modules`. |
| `test_launcher.py` | Launcher env + config binding: `KX_MCP_BUNDLES`, `KX_MCP_TRANSPORT`, and the `KX_MCP_AUTH*` settings parse correctly from env. |
| `test_logging.py` | `configure_logging`'s single brand-filtered root handler: surfaces every `kx_mcp*` logger (dotted children *and* underscore sibling trees like `kx_mcp_kdbx`), filters third-party noise, idempotent. |
| `test_packaging_deps.py` | Packaging guard: AST-scans each package's `src/` for first-party imports and asserts each is a declared dependency — the editable uv workspace masks undeclared sibling deps that would `ImportError` in a published wheel. |

## Prerequisites

None. No PyKX license, no running server, no IdP.

Fixtures come from the repo-root `conftest.py` (shared crypto: `keypair`, `mint`, `jwks_uri`) and
`tests/conftest.py` (`spawn_container` — not used here, but available). The `kx_mcp_example`
fixture bundle is resolved via the `tests/deterministic` pythonpath entry in `pyproject.toml`.

## How to run

```bash
# Just this tier (fast feedback — ~5 s)
just test-unit
# or: uv run pytest -m "not integration and not realidp"

# This directory only
uv run pytest tests/deterministic/unit/ -v

# A single file
uv run pytest tests/deterministic/unit/test_auth.py -v

# A single test
uv run pytest tests/deterministic/unit/test_auth.py -k test_static_wrong_issuer_rejected
```

## What's NOT tested here

- HTTP transport wiring and JWT validation through the FastMCP HTTP stack — those require a real
  subprocess (see `../integration/`).
- The full `AccessToken` contextvar → mounted tool path — in-process clients bypass the HTTP auth
  layer, so the contextvar is never set (see `../integration/test_auth_integration.py`
  `test_authenticated_principal_visible_in_mounted_tool`).
- Real IdP token acquisition and live JWKS validation (see `../realidp/`).
