# Configuring authentication & authorization

The complete front-to-back reference for securing a kx-mcp container: **inbound** authentication
(who is calling), **outbound** identity propagation (who the backend sees), and **authorization**
(what they may do). Each seam is opt-in and independently configured, so you can adopt them
incrementally — start with no auth at all, and harden path by path.

If you are deploying rather than configuring auth specifically, start with the
[deployment guide](deployment.md). (The design rationale behind each seam — container shape,
outbound token exchange, plain-kdb+ identity assertion, authorization — is maintained in the
project's internal design docs.)

## The zero-setup default

With nothing configured, the container runs **unauthenticated, single-principal** — the right
posture for local development and single-user STDIO bundling, where inbound and outbound identity
collapse to the developer:

```bash
uv run kx-mcp --bundles kdbx --transport stdio
```

No `KX_MCP_AUTH*` variables, no tokens, no IdP. Every request is anonymous; backends are reached
with whatever static credentials the backend config supplies (`KDBX_DB_USERNAME`/`_PASSWORD`, etc.).
Everything below is what you add when more than one principal is in the picture.

## The three seams at a glance

```
MCP client ──bearer──▶ [ inbound: KX_MCP_AUTH ] ──principal──▶ [ authz: KX_MCP_AUTHZ ]
                                                                      │ allow
                                                                      ▼
                                              [ outbound: <BACKEND>_OUTBOUND_STRATEGY ] ──▶ backend
```

| Seam | Selector variable | Default | Modes / strategies |
| --- | --- | --- | --- |
| Inbound authn | `KX_MCP_AUTH` | `unset` (off) | `unset` · `static` · `jwks` · `entra` |
| Authorization | `KX_MCP_AUTHZ` | `""` (route-only) | `""` · `static` · `kdbx_rbac` |
| Outbound identity | per backend, e.g. `KDBAI_DB_OUTBOUND_STRATEGY` | `""` (static creds) | `passthrough` · `service_account` · `rfc_8693` · custom |

The selector is the **bare** variable (`KX_MCP_AUTH`, not `KX_MCP_AUTH_MODE`); mode-specific detail
lives under the matching prefix (`KX_MCP_AUTH_*`, `KX_MCP_AUTHZ_*`).

---

## Inbound authentication (`KX_MCP_AUTH`)

The container validates the bearer on every request and exposes the result to backend tools as the
*principal* (`current_principal()` — it crosses the mount boundary). A bad token yields a clean
`401`, never a crash.

### `jwks` — validate against a live IdP (the production mode)

Validates RS256 bearers against the IdP's JWKS endpoint. Works with Keycloak, Entra ID, Auth0, or
any OIDC issuer that publishes JWKS.

```bash
KX_MCP_AUTH=jwks
KX_MCP_AUTH_JWKS_URI=https://idp.example/realms/quants/protocol/openid-connect/certs   # required
KX_MCP_AUTH_ISSUER=https://idp.example/realms/quants    # validated when set
KX_MCP_AUTH_AUDIENCE=kdbx-service                       # validated when set
KX_MCP_AUTH_RESOURCE_URL=https://mcp.example            # enables client discovery (below)
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `KX_MCP_AUTH_JWKS_URI` | — | **Required.** The IdP's JWKS endpoint. |
| `KX_MCP_AUTH_ISSUER` | — | Expected `iss` claim; validated if set. |
| `KX_MCP_AUTH_AUDIENCE` | — | Expected `aud` claim; validated if set. |
| `KX_MCP_AUTH_ALGORITHM` | `RS256` | Accepted JWT signing algorithm. |
| `KX_MCP_AUTH_REQUIRED_SCOPES` | — | Scopes the bearer must carry. |
| `KX_MCP_AUTH_RESOURCE_URL` | — | The container's own public base URL; enables RFC 9728 discovery. |

**Discovery — how clients find your IdP.** A bare verifier validates tokens but *advertises
nothing*: `/.well-known/oauth-protected-resource` 404s, and clients can't learn where to log in.
Setting `KX_MCP_AUTH_RESOURCE_URL` (to the URL clients actually connect to — proxy-aware) makes the
container advertise **RFC 9728 Protected Resource Metadata**, naming the issuer as the
authorization server. That's what lets Claude Code self-authenticate (discovery → dynamic client
registration → browser login) and what `kx auth login` discovers against. Leave it unset for
validate-only (the STDIO / no-public-URL posture).

Proven live: [`demos/claude-code-live-kdbai/`](../demos/claude-code-live-kdbai/) — Claude Code with
no pre-injected token discovers Keycloak via RFC 9728, registers itself via DCR, and logs the user
in through the browser.

### `entra` — Microsoft Entra ID as the login front door

Entra has **no open dynamic client registration**, so the discovery/DCR flow above can't work
against it directly. `entra` mode instead runs FastMCP's `AzureProvider` (an OAuth proxy): you
pre-register **one** app in Entra, and the container brokers the browser login on its behalf.

```bash
KX_MCP_AUTH=entra
KX_MCP_AUTH_RESOURCE_URL=http://localhost:8000    # the container's public URL
KX_MCP_AUTH_CLIENT_ID=<app-registration-client-id>
KX_MCP_AUTH_CLIENT_SECRET=<client-secret>
KX_MCP_AUTH_TENANT_ID=<tenant-guid>
KX_MCP_AUTH_REQUIRED_SCOPES=access                # required — the app's exposed scope name(s), unprefixed
# KX_MCP_AUTH_IDENTIFIER_URI=api://custom-uri     # only if you customised it (default api://{client_id})
```

`KX_MCP_AUTH_REQUIRED_SCOPES` is **mandatory** in `entra` mode (the app's exposed scope name(s),
unprefixed — e.g. `access`); the container refuses to start without it. The `AZURE_CLIENT_ID` /
`AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` names are accepted as aliases, so an existing Azure-app
`.env` works unchanged.

Proven live: [`demos/claude-code-live-kdbai-entra/`](../demos/claude-code-live-kdbai-entra/) —
including the wrinkles section in its `manual.md` (e.g. Entra's `prompt=select_account` silently
reusing a cached SSO session). If you only need to *validate* Entra-issued tokens (clients obtain
them elsewhere), plain `jwks` mode against the Entra JWKS endpoint is enough — that's what the
`AUTH_PROVIDER=entra` test lane does.

### `static` — a local public key (no IdP)

Validates RS256 bearers against a PEM public key you supply — useful for tests and self-minted
token setups, not production.

```bash
KX_MCP_AUTH=static
KX_MCP_AUTH_PUBLIC_KEY_PATH=/path/to/public.pem   # or KX_MCP_AUTH_PUBLIC_KEY with inline PEM
KX_MCP_AUTH_ISSUER=...      # optional claim validation, as in jwks mode
KX_MCP_AUTH_AUDIENCE=...
```

### `unset` — off (default)

No validation; anonymous principal. Empty and whitespace values collapse to `unset` too.

---

## Outbound identity (per backend)

Inbound answers "who called the container"; outbound decides **who the backend sees**. Each backend
selects a strategy via its own config fragment (the shared seam is
[`kx_auth_core.outbound`](../packages/kx-auth-core/src/kx_auth_core/outbound/strategies.py)).

| Strategy | The backend sees | Use when |
| --- | --- | --- |
| *(empty, default)* | The backend's static credentials | No per-user identity at the backend; local dev |
| `passthrough` | **The end user** — the inbound bearer is forwarded unchanged | Backend trusts the same issuer and enforces its own per-user ACL |
| `service_account` | **The container** — its own client-credentials token | Backend authorizes the workload, not the user |
| `rfc_8693` | The user, re-scoped — token exchanged at an STS | A real STS deployment (mock-tested; no current backend requires it) |

Passthrough's structural limit: one token, one audience — the inbound token's `aud` must match what
the backend expects, which caps passthrough at a single backend per token. `service_account` /
`rfc_8693` exist for the multi-backend case.

### KDB.AI (`KDBAI_DB_*`)

```bash
KDBAI_DB_OUTBOUND_STRATEGY=passthrough       # or service_account, or empty for static creds
# for service_account additionally:
KDBAI_DB_TOKEN_URL=https://idp.example/realms/quants/protocol/openid-connect/token
KDBAI_DB_CLIENT_ID=kdbai-service
KDBAI_DB_CLIENT_SECRET=<secret>
KDBAI_DB_AUDIENCE=kdbai-db                   # Keycloak: must match KDB.AI's OAUTH_CLIENT_ID
KDBAI_DB_SCOPES="api://<app-id>/.default"    # Entra: use scopes, leave AUDIENCE empty
```

Under `passthrough`, KDB.AI's own **server-side ACL** then differentiates callers off the
propagated `tenant`/`groups` claims — no container-side authz code involved. Proven live in both
qipc and against Entra: see [`packages/kx-mcp-kdbai/README.md`](../packages/kx-mcp-kdbai/README.md)
and the `KA.*` suite in [`tests/deterministic/realidp/kdbai/`](../tests/deterministic/realidp/kdbai/).

### Plain kdb-x — identity assertion, not token exchange (`KDBX_DB_ASSERT_IDENTITY`)

qIPC has no bearer to forward, so plain kdb+ is **not** a token-exchange backend. Instead the
container **ferries** the validated identity to q and q **promotes** it:

```bash
KDBX_DB_ASSERT_IDENTITY=true      # default false = single-principal, unchanged behaviour
KDBX_DB_USERNAME=<service-account>
KDBX_DB_PASSWORD=<service-account-pw>   # or KDBX_DB_PASSWORD_FILE (wins when set)
```

The container connects as a trusted service account (per-principal cached connections), and calls
`.kx.auth.bind` with the projected principal (structured fields + raw claims). The q side is the
[`kx.auth`](../modules/kx/auth/init.q) module (install with `just install-modules`): the host
process loads it, configures which claim paths promote to `groups`/`tenant`
(`.kx.auth.setClaims`), and supplies a policy to the default-deny `authorize`/`setPolicy` hook.
q-side setup detail: [`packages/kx-mcp-kdbx/README.md`](../packages/kx-mcp-kdbx/README.md)
§ Identity assertion.

---

## Authorization (`KX_MCP_AUTHZ`)

Authorization has two layers, distinguished by *where* the check runs and *what* it knows about
(the codebase calls them **PEP-1** and **PEP-2** — policy enforcement points 1 and 2):

- **The capability check (PEP-1)** — a data-agnostic check at the tool boundary (the `@authorize`
  decorator), applied **selectively**: only where a tool-class concern (write, admin, publish) is
  distinct from the backend's data gate. Undecorated tools are *route-only* — never checked here.
- **The data gate (PEP-2)** — the backend's own data check (q `.kx.auth.authorize`, KDB.AI ACL, or a
  downstream extension's native policy engine), authoritative for what data a caller sees, able to
  scope-down (filter) rather than just deny. For kdb-x the container-side explicit consult of this
  gate is opt-in via `KDBX_DB_DATA_GATE=true` (requires `KDBX_DB_ASSERT_IDENTITY=true`): before a
  query runs, the tool asks q `.kx.auth.entitled[action;tables]` on the bound per-principal handle
  and allows, returns a structured `permission_denied`, or scopes down to the entitled subset.
  Detail: [`packages/kx-mcp-kdbx/README.md`](../packages/kx-mcp-kdbx/README.md) § Configuration.

`KX_MCP_AUTHZ` selects the adapter behind the capability check:

| Value | Behaviour |
| --- | --- |
| `""` *(default)* | Route-only. Decorated tools allow; backends' data gates are the sole authority. |
| `static` | The built-in YAML capability-grant adapter (groups ∩ grant, keyed namespace → action). |
| `kdbx_rbac` | kdb-x adapter: delegates the `(action; resource)` decision to the q `.kx.auth` engine. |

```bash
KX_MCP_AUTHZ=static
KX_MCP_AUTHZ_POLICY_FILE=/etc/kx-mcp/capability-policy.yaml
KX_MCP_AUTHZ_GROUPS_CLAIM=groups     # which inbound claim carries groups (Entra: "roles" or a GUID mapping)
```

The policy file shape:

```yaml
kdbx:
  query: [admin]          # only the admin group may invoke the kdbx_run_sql_query tool
acme:
  publish: [data-eng, admin]
```

(`query` is the action the shipped kdb-x SQL tool declares — `@authorize(action="query",
resource="kdbx:sql")`; `acme:publish` illustrates the shape for your own extension's tool-class.)

Two rules worth internalising: a **decorated** action absent from the file is **denied** (a declared
capability concern with no grant means nobody is granted), and route-only is expressed by *not
decorating a tool* — never by omitting a line. A PEP-1 deny surfaces to the agent as a clean
structured `AuthorizationDenied` tool error; a PEP-2 data deny is the backend's own structured
denial — either way the agent can pivot without crashing.

---

## The `kx auth` CLI — the client-side companion

`kx auth` runs **where the agent/client runs**, not on the server, and exercises the same code
paths the container uses. Full reference:
[`packages/kx-auth-cli/README.md`](../packages/kx-auth-cli/README.md).

| Command | What it does |
| --- | --- |
| `kx auth login --server <url>` | RFC 9728 discovery against the MCP server → DCR → RFC 8628 device-code login; caches tokens at `~/.kx/credentials.json` (`KX_AUTH_CACHE` overrides) |
| `kx auth introspect <token>` | Validate a bearer with the same verification the container applies (`--json` for the envelope) |
| `kx auth exchange` | RFC 8693 token exchange from a shell (subject from `--subject` / `$KX_AUTH_TOKEN` / stdin / the login cache) |
| `kx auth assert` | Exercise the kdb+ identity-assertion projection (optional live qIPC bind via the `kx-auth-cli[qipc]` extra) |

Every subcommand emits a `--json` envelope and the stable exit-code contract:
`0` ok · `1` error · `2` usage · `3` auth-required · `4` denied.

The documented production path, end to end:

```bash
kx auth login --server http://mcp.example:8000/mcp --json    # discover IdP, log in, cache the token
# sanity-check what the server will see (the login envelope carries the access_token):
TOKEN=$(kx auth login --server http://mcp.example:8000/mcp --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
kx auth introspect "$TOKEN"
# then point your MCP client at the server with the bearer ($TOKEN)
```

## Audit

Every dispatch is logged on the `kx_mcp.audit` logger (INFO):
`subject / action / target / outcome`, with `action ∈ {tool_invoke, resource_read, prompt_get}` —
plus the authz decision + adapter when PEP-1 runs. Audit visibility is an entry-point job: the
`kx-mcp` launcher configures it; if you embed `make_parent` in your own glue, call
`configure_logging()` yourself. Tune with `KX_MCP_LOG_LEVEL`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `401 invalid_token` on every call | Issuer/audience mismatch: compare the token's `iss`/`aud` (`kx auth introspect`) against `KX_MCP_AUTH_ISSUER`/`_AUDIENCE`; check the JWKS URI is reachable *from the container*. |
| Client can't find the IdP (no login prompt) | `KX_MCP_AUTH_RESOURCE_URL` unset (no RFC 9728 advertisement), or set to a URL that differs from the one the client connects to (proxy). |
| Entra login skips the account picker | Entra reuses a cached SSO session despite `prompt=select_account` — see the wrinkles section in [`demos/claude-code-live-kdbai-entra/manual.md`](../demos/claude-code-live-kdbai-entra/manual.md). |
| Passthrough works for one backend, 401s at a second | The one-token-one-audience ceiling — use `service_account` (or an STS via `rfc_8693`) for multi-backend fan-out. |
| kdb-x tools fail after enabling `ASSERT_IDENTITY` | The q host hasn't loaded/configured `kx.auth` (`.kx.auth:use`kx.auth`; `setClaims`; a policy via `setPolicy`) — the default-deny gate rejects unbound/unauthorized calls. |
| PEP-1 denies a tool you expected to allow | The tool is decorated and its `(namespace, action)` has no grant in the policy file — a decorated action absent from the file is denied by design. |
