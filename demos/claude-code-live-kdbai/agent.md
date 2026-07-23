# Claude Code → live KDB.AI (agent path)

A **playbook for an LLM agent** — the counterpart to the operator [`manual.md`](manual.md). It is
consumed by Claude Code after it has self-authenticated via native discovery (manual.md Steps 1–4).
The agent drives the scenario by calling the container's MCP tools.

---

## Setup (human, once)

1. Run `demos/claude-code-live-kdbai/run.sh` (or follow manual.md Steps 1–3) to start the
   container and register `kx-kdbai-alice` without a token.
2. Start Claude Code **from the repo root**: `cd /path/to/kx-mcp-server-container && claude`
3. Run `/mcp` — you should see `kx-kdbai-alice · needs authentication`.
4. Hit **Authenticate**, log in as alice in the browser. `/mcp` should show `connected · 11 tools`.
5. Paste the scenario below into Claude Code.

---

## Scenario (paste this into Claude Code)

> You are connected to an MCP server called **`kx-kdbai-alice`** — the KX composition container,
> fronting a live OAuth KDB.AI database. You authenticated against it natively (no token was
> pre-supplied; you found the identity provider yourself via RFC 9728 discovery).
>
> Without being told the tool schemas up front:
>
> 1. **Discover** the available tools on `kx-kdbai-alice`.
> 2. Call **`kdbai_list_tables`** on the database `db_read`.
> 3. If tables are returned, call **`kdbai_query_data`** on `db_read/T1` to fetch a sample row.
> 4. Report what you found and confirm that the data came back through an authenticated,
>    identity-propagating chain — your token was validated inbound by the container and forwarded
>    to KDB.AI, which granted access based on alice's `tenant` and `groups` claims.

---

## Expected behaviour

- Tool list → `kdbai_*` tools (list_tables, query_data, table_info, similarity_search, hybrid_search, …).
- `kdbai_list_tables(db_read)` → `["T1"]` — alice's trader grant matches.
- `kdbai_query_data(db_read/T1)` → 1 row (the seed row from `seed.py`).

---

## Recorded result (2026-06-25)

Proven live against `kdbai-db 2.0.0-rc.2` + Keycloak 24.0. Claude Code self-authenticated via
the full RFC 9728 → DCR → auth-code browser flow with no pre-injected token, then called
`kdbai_list_tables(db_read)` → `["T1"]` and `kdbai_query_data(db_read/T1)` → 1 row.

---

## What this proves

Claude Code navigated the full agentic auth chain autonomously:

1. Hit `401` on `/mcp`.
2. Read `WWW-Authenticate` → fetched `/.well-known/oauth-protected-resource/mcp`.
3. Found `authorization_servers: [http://localhost:8080/realms/quants]`.
4. Did RFC 7591 DCR — registered itself as a new OAuth client with Keycloak.
5. Opened a browser for the auth-code flow — user logged in as alice.
6. Received a JWT, validated inbound by the container (`iss` + `aud` + signature).
7. Container forwarded the bearer to KDB.AI (`passthrough`) — KDB.AI granted access on alice's
   `tenant=quants`, `groups=[trader,viewer]` claims.

No pre-injected token. No hardcoded IdP address in the client config. The container's discovery
advertisement is the only hint Claude Code needed.

The deterministic backstop for the ACL result is the committed
`tests/deterministic/realidp/kdbai/` suite (KA.2 / KA.3): same identity propagation, programmatic
assertions, runs with `just test-kdbai`.
