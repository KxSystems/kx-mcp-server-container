# Claude Code → live KDB.AI via Entra ID (operator path)

Run the composition container as an **Entra-protected HTTP MCP server**
(`KX_MCP_AUTH=entra` → FastMCP's `AzureProvider`) against a real OAuth `kdbai-db`, and connect
**Claude Code** as the MCP client — with **no pre-supplied token**. Claude Code discovers the
identity provider itself and authenticates against Microsoft Entra ID.

**What this proves:** the same agentic-auth chain as the
[Keycloak demo](../claude-code-live-kdbai/manual.md), but against a real enterprise IdP that has
**no open Dynamic Client Registration** — so the container itself must broker the OAuth flow
(via `AzureProvider`) rather than just validating a token someone else minted.

```
Claude Code  --(no token)-->  container :8000
  [401 → AzureProvider's DCR-compatible facade → Entra login page]
  [→ auth-code flow → browser → alice logs in → token returned]
  --(Bearer: alice-token)-->  container :8000
     [KX_MCP_AUTH=entra validates alice's JWT: iss ✓  aud ✓  sig ✓ — same JWTVerifier as jwks mode]
  --> kdbai backend  --(passthrough: bearer-as-qipc-password)-->  kdbai-db
     [alice: tid=<ENTRA_TENANT_ID>, groups=[quants-trader OID, quants-viewer OID] → grant match → ["T1"]]
```

The alice-vs-bob ACL contrast is covered by the committed test suite
(`tests/deterministic/realidp/kdbai/test_kdbai_acl.py`, run via `just test-kdbai-entra`). This
demo's unique claim is the **interactive login chain** — the thing the tests can't prove because
they mint tokens via ROPC, not a browser.

---

## Why `KX_MCP_AUTH=entra`, not `jwks`

The Keycloak demo uses `KX_MCP_AUTH=jwks` (validate-only) because Keycloak allows anonymous
Dynamic Client Registration — Claude Code can register itself as an OAuth client on the fly.
**Entra has no open DCR.** A validate-only mode can check a bearer that already exists, but it
can't drive a login. `KX_MCP_AUTH=entra` selects FastMCP's `AzureProvider` (an `OAuthProxy`),
which presents Claude Code with a DCR-compatible facade while brokering the real login through
one **pre-registered** Entra app (`kdbai-service-mcp-agent`). Once a token comes back, validation
and everything downstream — `JWTVerifier`, passthrough, kdbai-db ACL — is identical to the
`jwks` path.

---

## Prerequisites

**Infrastructure** (one-time per machine):

```bash
# The kdbai-db image is registry-gated:
docker login registry.gitlab.com

# A kdb+ license is required for kdbai-db:
export KDB_LICENSE_B64=$(base64 ~/.kx/kc.lic)
```

**Provision Entra ID** (one-time per tenant — see
[`tests/deterministic/realidp/setup/entra/README.md`](../../tests/deterministic/realidp/setup/entra/README.md)
for the full app-registration requirements):

```bash
# Fill in tests/deterministic/realidp/envs/.env.entra (tenant ID, client ID, Graph secret,
# alice/bob/root passwords), then SOURCE it before running the provisioner — the script reads
# these via os.environ, so a filled-in-but-not-sourced file is silently ignored (ENTRA_TENANT_ID
# etc. fall back to "" and the run fails or provisions the wrong thing):
set -a; source tests/deterministic/realidp/envs/.env.entra; set +a
uv run tests/deterministic/realidp/setup/entra/entra_setup.py
```

This provisions the `kdbai-service-mcp-agent` app (with the `access` API scope, groups claim,
ROPC enabled, and the **`http://localhost:8000/auth/callback` redirect URI** that `AzureProvider`
needs), the quants/risk/manager groups, and alice/bob/root users. It writes the non-secret
provisioned values (group Object IDs, domain, etc.) back into `.env.entra`.

**Start + seed the Entra-trusting kdbai-db:**

```bash
# The compose file mounts ./kdbai-data and ./acl-data (relative to setup/entra/). kdbai-db runs
# as the 'nobody' user inside the container, so these must exist and be world-writable BEFORE
# `up -d` — otherwise the container fails to start (or silently can't persist data/grants).
mkdir -p tests/deterministic/realidp/setup/entra/kdbai-data \
         tests/deterministic/realidp/setup/entra/acl-data
chmod 777 tests/deterministic/realidp/setup/entra/kdbai-data \
          tests/deterministic/realidp/setup/entra/acl-data

docker compose -f tests/deterministic/realidp/setup/entra/docker-compose.yaml up -d
uv run python tests/deterministic/realidp/setup/entra/seed.py
# Creates db_read/T1. Grant: quants-trader group → read db_read.
```

**Quick verify:**

```bash
curl -s -o /dev/null -w '%{http_code}' http://localhost:8081/healthz
# → 401 (REST API up, auth-gated — this is the "up" signal, not 200)
```

---

## Quick start — turnkey path

Once the prerequisites above are done, a single command starts the container and registers it
with Claude Code:

```bash
# From the repo root:
demos/claude-code-live-kdbai-entra/run.sh
```

It starts the container and registers `kx-kdbai-entra` **without a token**. Then:

1. Start Claude Code from the repo root: `cd /path/to/kx-mcp-server-container && claude`
2. Run `/mcp` — you should see `kx-kdbai-entra · needs authentication`
3. Hit **Authenticate** — a browser window opens, log in as **alice** (`alice@<ENTRA_DOMAIN>` /
   `$ENTRA_PASSWORD_ALICE`)
4. Browser shows "Authentication successful" → `/mcp` shows `connected`
5. Paste the scenario from [`agent.md`](agent.md)

**Steps 1–4 below are the manual equivalent**, explained step by step.

---

## Step 1 — start the container in Entra OAuth-proxy mode

```bash
set -a
source tests/deterministic/realidp/envs/.env.entra
source demos/claude-code-live-kdbai-entra/entra.env
set +a

KX_MCP_AUTH_RESOURCE_URL=http://localhost:8000 \
  uv run kx-mcp --bundles kdbai --transport streamable-http --host 127.0.0.1 --port 8000
```

> **The port matters.** `KX_MCP_AUTH_RESOURCE_URL` must be `http://localhost:8000` — that's the
> exact redirect URI (`http://localhost:8000/auth/callback`) registered on the Entra app by
> `entra_setup.py`. A different port fails with `AADSTS50011: redirect URI mismatch`.

The startup log shows the Entra proxy initializing:

```
inbound auth via Entra OAuth proxy: tenant=<ENTRA_TENANT_ID> client_id=<ENTRA_CLIENT_ID> base_url=http://localhost:8000 scopes=['access']
kdbai outbound strategy: passthrough (bearer-as-qipc-password)
backend 'kdbai' mounted
Using non-secure cookies for development; deploy with HTTPS for production
```

The cookie warning is expected and harmless for a **localhost** demo — Entra accepts `http`
redirects for `localhost` specifically, so no HTTPS tunnel is needed here.

---

## Step 2 — register with Claude Code (no token)

```bash
claude mcp remove kx-kdbai-entra 2>/dev/null || true
claude mcp add --transport http kx-kdbai-entra http://localhost:8000/mcp
```

No `--header`. Claude Code will discover and fetch the token itself, against Entra.

---

## Step 3 — start Claude Code and authenticate

Start Claude Code **from the repo root**:

```bash
cd /path/to/kx-mcp-server-container && claude
```

Run `/mcp`. You should see:

```
kx-kdbai-entra  http://localhost:8000/mcp  needs authentication
```

Hit **Authenticate**. A browser window opens to the Entra login page. Log in as **alice**
(`alice@<ENTRA_DOMAIN>` / `$ENTRA_PASSWORD_ALICE`). The browser shows "Authentication
successful" → Claude Code stores the token.

Run `/mcp` again — you should see:

```
kx-kdbai-entra  http://localhost:8000/mcp  connected · 11 tools
```

---

## Step 4 — drive the scenario

Paste the scenario from [`agent.md`](agent.md) into Claude Code.

Expected: alice calls `kdbai_list_tables(db_read)` → `["T1"]`, then
`kdbai_query_data(db_read/T1)` → 1 row. The container audit log shows the dispatch:

```
audit subject=<client-id-azp> action=tool_invoke target=kdbai_list_tables outcome=ok
```

---

## Step 5 — teardown

```bash
claude mcp remove kx-kdbai-entra
# Ctrl-C the container terminal
docker compose -f tests/deterministic/realidp/setup/entra/docker-compose.yaml down
```

---

## Entra wrinkles

Everything below was hit live getting this demo working. Documented here so the next person
doesn't spend an hour rediscovering them.

**Stale OAuth-cache collision.** OAuth state is keyed on the **MCP server URL**, not the server
name. If you previously ran the Keycloak demo (or a prior Entra session) on
`http://localhost:8000/mcp`, a stale Keycloak (or expired Entra) token can be silently rejected
by kdbai-db — no useful error, just repeated "needs authentication". `run.sh` warns if it finds
one. Two caches are in play: Claude Code's own (reset with `claude mcp remove` + re-add, then
re-authenticate) plus its `~/.claude/mcp-needs-auth-cache.json`; and — only if you've also run
`kx auth login` against this URL — the `kx` CLI's token cache `~/.kx/credentials.json`
(Claude Code never reads that file). To clear the file-backed entries manually (`jq` writes to
stdout, so filter to a temp file and move it back):

```bash
tmp=$(mktemp)
jq 'del(.["kx-kdbai-entra"])' ~/.claude/mcp-needs-auth-cache.json > "$tmp" \
  && mv "$tmp" ~/.claude/mcp-needs-auth-cache.json
tmp=$(mktemp)
jq 'del(.["http://localhost:8000/mcp"])' ~/.kx/credentials.json > "$tmp" \
  && mv "$tmp" ~/.kx/credentials.json          # kx auth CLI cache only
```
Then restart Claude Code and re-authenticate.

**The Entra login may silently reuse a cached SSO session instead of prompting.**
`AzureProvider.authorize()` appends `prompt=select_account` to the auth URL, which is meant to
show an account picker — but with only **one** active Microsoft session in the browser, Entra can
skip the picker entirely and silently sign you in as whatever account is already cached (e.g. your
own corporate/admin account, from provisioning the app earlier in the same browser). Symptom: the
login "just works" with no visible prompt, `/mcp` shows `connected`, but every tool call behaves as
an identity with **no kdbai groups** — `kdbai_list_tables(db_read)` returns `{"tables":[]}` instead
of `["T1"]`, and admin-only calls (`kdbai_list_databases`, `kdbai_system_info`, …) return
`access not allowed`. This looks like an ACL bug but isn't — the grants are fine (verify with the
root token: `session.admin.get_all_grants()` shows the `quants-trader` → `db_read` grant intact);
the token reaching kdbai-db just isn't alice's.

Fix — force a fresh login as alice:
1. Clear Claude Code's cached token for this URL (same commands as above) and
   `claude mcp remove kx-kdbai-entra` / re-`add`.
2. Sign out of the cached Microsoft session first — either open the Authenticate link in an
   **incognito/private window**, or visit
   `https://login.microsoftonline.com/common/oauth2/v2.0/logout` in the same browser, before
   hitting Authenticate again.
3. When the real Entra login page appears this time, explicitly type `alice@<ENTRA_DOMAIN>` —
   don't let it autofill or silently continue with a different cached account.

**Redirect URI must match exactly.** `KX_MCP_AUTH_RESOURCE_URL` + `/auth/callback` must equal a
**Web** platform redirect URI registered on the Entra app. `entra_setup.py` registers
`http://localhost:8000/auth/callback` by default (override via `ENTRA_REDIRECT_URI`). Changing
the demo's port without updating the portal registration fails with `AADSTS50011`.

**v2.0 access tokens required.** `entra_setup.py` sets `requestedAccessTokenVersion=2` on the
app — required by `AzureProvider`. This makes `aud` the bare client GUID and `iss` the
`.../v2.0` form. If you see a 401 with an issuer/audience mismatch, decode the token at
[jwt.ms](https://jwt.ms) and confirm `ver: "2.0"`.

**The container needs a live client secret.** Unlike `jwks` mode, `entra` mode authenticates to
Entra as a confidential client — `ENTRA_CLIENT_SECRET` in `.env.entra` must be the
`fastmcp-auto-generated` secret `entra_setup.py` creates (Graph only returns a secret's value
once, at creation time — if it's blank, re-run the provisioner or rotate the secret in the
portal).

**"needs authentication" + "✓ authenticated" is not a contradiction** (same as the Keycloak
demo) — the token is stored but the transport hasn't reconnected yet. Hit **Re-authenticate**.

---

## Wrinkles

- **Audit `subject=<client-id-azp>`, not the human** — same as the Keycloak demo. The human
  identity (alice) flows to KDB.AI via `tid` + `groups` in the forwarded bearer, not the audit
  subject.
- **Passthrough is not least-privilege** — the full bearer is forwarded to KDB.AI.
- **Bridged networking, not host mode** — unlike the Keycloak compose, the Entra kdbai-db compose
  uses normal port mapping (`:8081`, `:8082`) because Entra's issuer/JWKS are public URLs, not
  `localhost` — no `network_mode: host` Linux-ism here.
- **Restart the container if you re-authenticate as a different user** — the kdbai backend caches
  qipc Sessions per-`sub`. Restart the container to clear a stale cached session.
