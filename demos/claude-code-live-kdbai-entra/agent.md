# Claude Code → live KDB.AI via Entra ID (agent path)

A **playbook for an LLM agent** — the counterpart to the operator
[`manual.md`](manual.md). It is consumed by Claude Code after it has self-authenticated against
Microsoft Entra ID (manual.md Steps 1–3). The agent drives the scenario by calling the
container's MCP tools.

---

## Setup (human, once)

1. Run `demos/claude-code-live-kdbai-entra/run.sh` (or follow manual.md Steps 1–2) to start the
   container and register `kx-kdbai-entra` without a token.
2. Start Claude Code **from the repo root**: `cd /path/to/kx-mcp-server-container && claude`
3. Run `/mcp` — you should see `kx-kdbai-entra · needs authentication`.
4. Hit **Authenticate**, log in as alice in the Entra browser page. `/mcp` should show
   `connected · 11 tools`.
5. Paste the scenario below into Claude Code.

---

## Scenario (paste this into Claude Code)

> You are connected to an MCP server called **`kx-kdbai-entra`** — the KX composition container,
> fronting a live OAuth KDB.AI database. You authenticated against it through **Microsoft Entra
> ID** (no token was pre-supplied; the container's `AzureProvider` brokered the login for you).
>
> Without being told the tool schemas up front:
>
> 1. **Discover** the available tools on `kx-kdbai-entra`.
> 2. Call **`kdbai_list_tables`** on the database `db_read`.
> 3. If tables are returned, call **`kdbai_query_data`** on `db_read/T1` to fetch a sample row.
> 4. Report what you found and confirm that the data came back through an authenticated,
>    identity-propagating chain — your Entra token was validated inbound by the container and
>    forwarded to KDB.AI, which granted access based on alice's `tid` (tenant) and `groups`
>    (Object ID) claims.

---

## Expected behaviour

- Tool list → `kdbai_*` tools (list_tables, query_data, table_info, similarity_search, hybrid_search, …).
- `kdbai_list_tables(db_read)` → `["T1"]` — alice's quants-trader group grant matches.
- `kdbai_query_data(db_read/T1)` → 1 row (the seed row from `setup/entra/seed.py`).

---

## What this proves

Claude Code navigated the agentic auth chain against a real enterprise IdP with no open DCR:

1. Hit `401` on `/mcp`.
2. The container's `AzureProvider` presented a DCR-compatible facade — Claude Code registered
   against it without needing Entra itself to support anonymous registration.
3. Opened a browser for the Entra login page — user logged in as alice.
4. Received a JWT, validated inbound by the container (`iss` + `aud` + signature — the same
   `JWTVerifier` the `jwks` mode uses underneath `AzureProvider`).
5. Container forwarded the bearer to KDB.AI (`passthrough`) — KDB.AI granted access on alice's
   `tid=<ENTRA_TENANT_ID>`, `groups=[quants-trader OID, quants-viewer OID]` claims.

There's no pre-injected token, no ROPC, and no hardcoded credential in the client config. The
container's `AzureProvider` is the only thing standing between Claude Code and a real interactive
Entra login.

The deterministic backstop for the ACL result (including the alice-vs-bob denial contrast) is
the committed `tests/deterministic/realidp/kdbai/test_kdbai_acl.py` suite (KA.1–KA.7), which
runs with `just test-kdbai-entra` — same identity propagation, programmatic assertions, no
browser required.
