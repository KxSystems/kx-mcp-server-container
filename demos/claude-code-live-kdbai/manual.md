# Claude Code → live KDB.AI (operator path)

Run the composition container as a **jwks-protected, discovery-advertising HTTP MCP server**
against a real OAuth `kdbai-db`, and connect **Claude Code** as the MCP client — with **no
pre-supplied token**. Claude Code discovers the identity provider itself and authenticates.

**What this proves:** RFC 9728 → DCR → auth-code browser flow, end-to-end, with a live Keycloak
and a live KDB.AI backend. The container advertises the IdP; Claude Code finds it, registers
itself as an OAuth client, opens a browser, and connects — all autonomously.

```
Claude Code  --(no token)-->  container :8000
  [401 + WWW-Authenticate → GET /.well-known/oauth-protected-resource/mcp]
  [→ authorization_servers: http://localhost:8080/realms/quants]
  [→ DCR: POST /realms/quants/clients-registrations/openid-connect]
  [→ auth-code flow → browser → alice logs in → token returned]
  --(Bearer: alice-token)-->  container :8000
     [KX_MCP_AUTH=jwks validates alice's JWT: iss ✓  aud ✓  sig ✓]
  --> kdbai backend  --(passthrough: bearer-as-qipc-password)-->  kdbai-db
     [alice: tenant=quants, groups=[trader,viewer] → grant match → ["T1"]]
```

The ACL differentiation (alice vs bob) is covered by the committed test suite
(`tests/deterministic/realidp/kdbai/`, KA.1–KA.9). This demo's unique claim is the discovery
chain — the thing the tests can't prove because they use pre-minted tokens.

---

## Prerequisites

**Infrastructure** (one-time per machine):

```bash
# The kdbai-db image is registry-gated:
docker login registry.gitlab.com

# A kdb+ license is required for kdbai-db:
export KDB_LICENSE_B64=$(base64 ~/.kx/kc.lic)
```

**Start the backend services:**

```bash
cd tests/deterministic/realidp/setup/keycloak
KDB_LICENSE_B64=$KDB_LICENSE_B64 docker compose --profile backends up -d
```

Wait for both Keycloak and kdbai-db to be healthy (~30 s):

```bash
docker compose ps
# Both should show "healthy".
```

**Provision Keycloak realms, clients, and users:**

```bash
# From the keycloak/ directory:
uv run keycloak_setup.py keycloak_config.json
```

This creates the `quants`, `risk`, and `manager` realms; the `kdbai-service` public client in
each; alice/bob (quants), charlie (risk), root (manager). Crucially it also configures the three
Keycloak DCR policies that allow Claude Code to self-register (see "Native discovery wrinkles"
below).

If you're re-provisioning an existing Keycloak:

```bash
uv run keycloak_setup.py keycloak_config.json --delete-first
```

**Create databases and ACL grants:**

```bash
uv run seed.py
# Creates db_read/T1. Grant: quants/trader → read db_read.
```

**Quick verify:**

```bash
curl -sf http://localhost:8080/health/ready && echo "Keycloak OK"
curl -sf http://localhost:8082/healthz && echo "kdbai-db OK"
```

---

## Quick start — turnkey path

Once the prerequisites above are done, a single command starts the container and registers it
with Claude Code:

```bash
# From the repo root:
demos/claude-code-live-kdbai/run.sh
```

It starts the container and registers `kx-kdbai-alice` **without a token**. Then:

1. Start Claude Code from the repo root: `cd /path/to/kx-mcp-server-container && claude`
2. Run `/mcp` — you should see `kx-kdbai-alice · needs authentication`
3. Hit **Authenticate** — a browser window opens, log in as **alice** (password: `alice123`)
4. Browser shows "Authentication successful" → `/mcp` shows `connected`
5. Paste the scenario from [`agent.md`](agent.md)

**Steps 1–5 below are the manual equivalent**, explained step by step.

---

## Step 1 — start the container in discovery mode

Source the shared env, then launch the container with `KX_MCP_AUTH_RESOURCE_URL` set — this is
what enables RFC 9728 discovery advertisement.

```bash
set -a; source demos/claude-code-live-kdbai/kdbai.env; set +a

KX_MCP_AUTH_RESOURCE_URL=http://localhost:8000 \
  uv run kx-mcp --bundles kdbai --transport streamable-http --host 127.0.0.1 --port 8000
```

The startup log shows the discovery advertisement:

```
inbound auth advertising discovery: resource=http://localhost:8000 authorization_server=http://localhost:8080/realms/quants
inbound auth enabled: mode=jwks issuer=http://localhost:8080/realms/quants audience=kdbai-service
kdbai outbound strategy: passthrough (bearer-as-qipc-password)
backend 'kdbai' mounted
```

> **If the container exits immediately** with `Authentication error`: check that
> `KDBAI_DB_OUTBOUND_STRATEGY=passthrough` is set. The pre-flight does an authenticated ping —
> there is no anonymous fallback in passthrough mode.

---

## Step 2 — verify the discovery advertisement

```bash
# RFC 9728 Protected Resource Metadata — names the quants realm as the AS:
curl -s http://localhost:8000/.well-known/oauth-protected-resource/mcp | python3 -m json.tool
# → { "resource": "http://localhost:8000/mcp", "authorization_servers": ["http://localhost:8080/realms/quants"] }

# A bare call returns 401 with a WWW-Authenticate pointer to the PRM:
curl -si -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | grep -i www-authenticate
# → WWW-Authenticate: Bearer resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"
```

---

## Step 3 — register with Claude Code (no token)

```bash
# From the repo root:
claude mcp remove kx-kdbai-alice 2>/dev/null || true
claude mcp add --transport http kx-kdbai-alice http://localhost:8000/mcp
```

No `--header`. Claude Code will discover and fetch the token itself.

---

## Step 4 — start Claude Code and authenticate

Start Claude Code **from the repo root** (the registration is keyed to this path in
`~/.claude.json`):

```bash
cd /path/to/kx-mcp-server-container && claude
```

Run `/mcp`. You should see:

```
kx-kdbai-alice  http://localhost:8000/mcp  needs authentication
```

Hit **Authenticate**. Claude Code:
1. Gets `401` → reads `WWW-Authenticate` → fetches RFC 9728 PRM → finds the quants realm.
2. Fetches Keycloak AS metadata → finds `registration_endpoint` and `authorization_endpoint`.
3. Does RFC 7591 Dynamic Client Registration — registers itself as a new OAuth client.
4. Starts the auth-code flow → opens a browser window.
5. Log in as **alice** (password: `alice123`).
6. Browser shows "Authentication successful" → Claude Code stores the token.

Run `/mcp` again — you should see:

```
kx-kdbai-alice  http://localhost:8000/mcp  connected · 11 tools
```

---

## Step 5 — drive the scenario

Paste the scenario from [`agent.md`](agent.md) into Claude Code.

Expected: alice calls `kdbai_list_tables(db_read)` → `["T1"]`, then
`kdbai_query_data(db_read/T1)` → 1 row. The container audit log shows the dispatch:

```
audit subject=kdbai-service action=tool_invoke target=kdbai_list_tables outcome=ok
```

---

## Step 6 — teardown

```bash
claude mcp remove kx-kdbai-alice
# Ctrl-C the container terminal
cd tests/deterministic/realidp/setup/keycloak && docker compose down
```

---

## Native discovery wrinkles

Everything below was hit live (2026-06-25) getting Claude Code to self-authenticate. Documented
here so the next person doesn't spend an hour rediscovering them.

**Claude Code uses auth-code flow, not device-code.** `kx auth login` uses RFC 8628 device-code
(a URL + user code, no redirect). Claude Code uses standard OAuth authorization code + browser
redirect. Both prove the discovery chain. They are different flows and require different Keycloak
config.

**Claude Code does Dynamic Client Registration (DCR) before the auth-code flow.** It finds the
`registration_endpoint` in Keycloak's AS metadata and registers itself as a new OAuth client
automatically (RFC 7591). Keycloak does not allow anonymous DCR by default. Three policies block
it in sequence:

1. **Trusted Hosts** — blocks DCR from untrusted source hosts. Fix: set
   `host-sending-registration-request-must-match: false`, `client-uris-must-match: true`. Note:
   Keycloak requires at least one of the two checks to be enabled — you cannot disable both.

2. **Allowed Client Scopes** — blocks DCR requests that include scopes outside the allowed list.
   Fix: explicitly list `openid`, `profile`, `email`, `roles`, `web-origins`, `acr`,
   `offline_access` in the anonymous policy's `allowed-client-scopes`.

3. **Token audience mismatch** — DCR registers a new client with a generated ID (e.g.
   `848f368f-…`). Keycloak issues a token with `aud: ["848f368f-…"]`. The container validates
   `KX_MCP_AUTH_AUDIENCE=kdbai-service` — that fails. Fix: add a hardcoded audience mapper on a
   realm default client scope (`kdbai-resource-audience`) so every token (including DCR clients)
   carries `kdbai-service` in `aud`. The scope must be created **before** configuring the Allowed
   Client Scopes policy, because it is a realm default that Claude Code includes in its DCR
   request scope — so it must be in the allowed list.

All three fixes are in `keycloak_setup.py` and applied automatically on provisioning.

**Keycloak 24 removed the Client Registration Policies page from the admin UI.** Use the REST
API or `keycloak_setup.py`:

```bash
curl -s -H "Authorization: Bearer $KC_TOKEN" \
  "http://localhost:8080/admin/realms/quants/components?type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy" | \
  python3 -c "import sys,json; [print(c['id'], c['subType'], c['name']) for c in json.load(sys.stdin)]"
```

**Keycloak component IDs change on restart.** Always re-query by name, not UUID, if patching
manually.

**"needs authentication" + "✓ authenticated" is not a contradiction.** After the browser login,
the `/mcp` panel may briefly show `Status: needs authentication` but `Auth: ✓ authenticated`.
The token is stored but the transport hasn't reconnected yet. Hit **Re-authenticate** or escape
and retry.

---

## Wrinkles

- **Audit `subject=kdbai-service`, not the human** — the audit line keys on `azp` (the OAuth
  client id). The human identity (`alice`) flows to KDB.AI via `tenant` + `groups` in the
  forwarded bearer.
- **Passthrough is not least-privilege** — the full bearer is forwarded to KDB.AI. In production
  you'd scope the token at the AS before forwarding.
- **`network_mode: host` in the compose file** — kdbai-db uses host networking so
  `localhost:8080` in the JWT `iss` resolves to Keycloak from inside the container.
- **Restart the container if you re-authenticate as a different user** — the kdbai backend caches
  qipc Sessions per-`sub`. A new token for a different user won't evict the old cached session.
  Restart the container to clear it.
