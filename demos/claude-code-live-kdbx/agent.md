# Claude Code → live kdb-x, two RBAC sets (agent path)

A **playbook for an LLM agent** — the counterpart to the operator [`manual.md`](manual.md). It is
consumed by an MCP-client LLM (Claude Code) configured, per manual.md, with the jwks-protected
container as an HTTP MCP server it authenticated to (token-injection with a persona bearer). The
agent drives the scenario by calling the container's MCP tools; nothing here runs on its own.

## Setup (human, once)

1. Bring up the kdb-x host + container (manual.md Steps 1–2) and connect Claude Code as **alice**
   (manual.md Step 3).
2. Confirm `claude mcp list` shows `kx-kdbx` connected and `/mcp` lists the `kdbx_*` tools.

## Scenario (give this to the agent)

> You are connected to the `kx-kdbx` MCP server — the KX composition container, fronting a kdb-x
> database. Your access is authenticated and your identity is asserted to the database on every call.
> Without being told the tool schemas up front:
>
> 1. **Discover** the available tools (expect a SQL query tool, `kdbx_run_sql_query`).
> 2. Read some trades: run `SELECT sym, price, size FROM trades WHERE sym = 'AAPL'`. Report the rows.
> 3. Try a write: run `INSERT INTO trades VALUES ('AAPL', 199.0, 50)`. Report what happens.
> 4. Summarise as a short table: the tool + action you used, and the outcome (rows / denial / error)
>    of each step. If anything is denied, quote the denial message and say which layer it came from.

Then the human swaps the connection to **bob** (manual.md Step 4) and re-runs the same scenario.

## Expected behaviour

- The agent discovers `kdbx_run_sql_query` from the server, not from this doc.
- **As alice** (`viewer`+`trader`):
  - the `SELECT` returns AAPL rows (e.g. `187.45 / 100`, `188.10 / 120`) — she clears the capability
    check (`query`/`kdbx.sql`) *and* the data gate (`read`/`data.trades`);
  - the `INSERT` is rejected by the SQL write-keyword blocklist (`Query contains dangerous keyword:
    INSERT`) — a tool-level guardrail, before any auth.
- **As bob** (`viewer`):
  - the same `SELECT` returns `status: error`, `error_type: permission_denied`, message
    *"Access denied by the data layer: denied: <sub> not permitted read on data.trades"* — bob passed the
    **capability** check (he *may* invoke the SQL tool) but the **data** gate denied the `trades`
    entitlement. The agent should report this as a data-layer denial and **not** crash or retry blindly;
  - the `INSERT` is rejected by the blocklist, same as alice.

## What this proves

Two **independent semantic RBAC sets** enforced over one `.kx.auth` policy engine, off a single
asserted identity:

- **The capability check (PEP-1, container-side, MCP-semantic):** *may this subject invoke this
  tool?* — `query` on `kdbx.sql`, over a capability grant set.
- **The data gate (PEP-2, q-side, data-semantic):** *for this data, what is permitted?* — `read` on
  `data.trades`, over a data grant set, able to deny (and, later, scope-down).

bob's divergent outcome — capability allowed, data denied — shows that the two layers are genuinely
separate, and even a route-only tool with no capability grant of its own is still data-gated. The
SQL write-keyword blocklist remains the floor beneath both. (The capability check is the
`@authorize(action="query", resource="kdbx.sql")` decorator over the `kx_auth_core.authz` `kdbx_rbac`
adapter, active via `KX_MCP_AUTHZ=kdbx_rbac`; a capability-check deny raises a clean
`AuthorizationDenied` tool error, distinct from the data gate's structured `permission_denied`
envelope.)
