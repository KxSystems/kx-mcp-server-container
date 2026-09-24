# Real-backend test harness

**Requires a live Identity Provider or backend.** Tests are marked `@pytest.mark.realidp` (plus a
per-lane marker) and excluded from the default/CI run — `just test` never triggers them.

> **Run all commands from the repo root** (`kx-mcp-server-container/`).

## Lanes

| Lane | Dir | What it tests | Marker | Recipe |
|---|---|---|---|---|
| Inbound auth / Keycloak | `idp/` | Inbound auth 2.38–2.42 (provider-agnostic) + the `oidc_proxy` authorization-code flow 2.51–2.53 (Keycloak-only) | `realidp` | `just test-keycloak` |
| Inbound auth / Entra | `idp/` (`AUTH_PROVIDER=entra`) | Entra as a second identity provider for the container's inbound path | `realidp` | `just test-entra` |
| KDB.AI OAuth ACL | `kdbai/` | KDB.AI OAuth qIPC ACL (KA.1–KA.9) | `realidp and kdbai` | `just test-kdbai` |
| KDB-X identity ferry | `kdbx/` | Validated identity assertion and q-side authorization against a real q process | `realidp and kdbx` | `just test-kdbx` |

> **Cross-IdP coverage (Entra).** The inbound-auth suite (`idp/test_inbound_auth.py`, see
> [`idp/README.md`](idp/README.md) for test-by-test detail) is provider-agnostic via the
> `TokenProvider` seam (`AUTH_PROVIDER=keycloak|entra`). The Entra path uses per-persona ROPC
> against `kdbai-service-mcp-agent` — provisioned by
> [`setup/entra/entra_setup.py`](setup/entra/entra_setup.py). See
> [`setup/entra/README.md`](setup/entra/README.md) for the bring-up steps. Wrong-issuer (test
> 2.41) is skipped under single-tenant Entra; it auto-activates once `ENTRA_TENANT_ID_RISK` is set.

## Structure

```
realidp/
  conftest.py          — shared IdP fixtures: _require_idp (autouse), container_url,
                         personas / token_provider / make_token / *_token
  _spawn.py            — _free_port, _wait_until_listening, _spawn_container,
                         _spawn_oidc_proxy_container (fixed-port oidc_proxy spawn)
  envs/
    .env.keycloak.example
    .env.kdbai.example
    .gitignore

  idp/                 — shared IdP layer (Python test code, provider-agnostic)
    providers/
      base.py          — TokenProvider ABC
      factory.py       — select_provider(personas) — AUTH_PROVIDER dispatch
      keycloak.py      — password-grant token acquisition
      entra.py         — EntraTokenProvider (password/client_credentials/external_token flows)
    fixtures/tokens.py — session-scoped token fixtures per persona
    personas.yaml      — alice, bob, charlie, root
    helpers.py         — assert_authenticated, assert_principal_visible, assert_discovery_advertised,
                         assert_rejected_401
    test_inbound_auth.py — @pytest.mark.realidp tests 2.38–2.42 (provider-agnostic)
    oidc_proxy_driver.py — drives a real authorization-code flow against KX_MCP_AUTH=oidc_proxy
    test_oidc_proxy_code_flow.py — @pytest.mark.realidp tests 2.51–2.53 (Keycloak-only)
    README.md          — test-by-test detail + expected output (Keycloak + Entra)

  setup/               — local infra + IdP provisioning (no Python test code)
    keycloak/
      docker-compose.yaml  — postgres + keycloak + profile-gated kdbai-db (backends profile)
      keycloak_config.json — now also provisions kx-mcp-proxy (proxy_client), a confidential
                             client with a port-exact redirect URI for the oidc_proxy code flow
      keycloak_setup.py
      seed.py              — operator pre-step: creates databases, tables, and ACL grants
      .gitignore           — kdbai-data/, acl-data/ (volume dirs created at runtime)
    entra/
      README.md            — app-registration steps + EntraTokenProvider implementation checklist

  kdbai/               — kdbai backend lane (Python test code only)
    conftest.py        — _require_kdbai_db, kdbai_container_url, kdbai_manager_container_url
    helpers.py         — KA.* ACL assertions
    test_kdbai_acl.py  — @pytest.mark.realidp @pytest.mark.kdbai tests KA.1–KA.9

  kdbx/                — kdb-x identity-ferry lane against a throwaway q process
    conftest.py
    kdbx_ferry_host.q
    test_kdbx_ferry.py
```

Shared fixtures (`_require_idp`, `container_url`, `*_token`) are registered at
`realidp/conftest.py` — the common ancestor of all public lanes — and inherited automatically.
Each lane's `conftest.py` adds only lane-specific fixtures.
