# kx-auth-core

The lean, deliberately **fastmcp-free** auth core shared by the KX MCP composition container
(`kx-mcp-core`), the backend bundles, and the client-side `kx auth` CLI (`kx-auth-cli`). It holds
the auth *mechanisms* so every consumer binds to one implementation:

- **Inbound config + verification** — `AuthSettings` (the `KX_MCP_AUTH*` contract) and a
  [joserfc](https://jose.authlib.org/) `verify_token` returning a categorised verdict
  (`ok` / `error` / `auth_required` / `denied`). joserfc is the same JOSE library FastMCP's
  `JWTVerifier` uses, so server and CLI verification cannot drift.
- **Outbound identity** — `exchange(config, subject_token)` over the
  `register_outbound_strategy` registry (`passthrough` / `rfc_8693` / `service_account` / custom),
  with the `OutboundConfig` / `OutboundCredential` shapes. Pure token→credential; no fastmcp.
- **Authorization contract** — `AuthzRequest` / `AuthzDecision` (boolean-first, with an
  `obligations` slot for scope-down), the `AuthzAdapter` protocol, and the
  `register_authz_adapter` / `decide` registry behind the container's `@authorize` seam.
- **Identity assertion projection** — `project_principal` / `project_from_claims`, the
  fastmcp-free (and pykx-free) claims→dict projection the kdb-x identity-assertion path ferries
  to q.

## Who should depend on it

Backend bundles (for `exchange` and the authz contract) and client-side tooling (the `kx auth`
CLI). If you are assembling a *server*, depend on `kx-mcp-core` instead — it re-exports these
seams. Two invariants to respect when contributing: this package never imports `fastmcp` (it ships
where the agent/client runs), and `verify_token` must stay in agreement with the container's
`JWTVerifier` (pinned by contract tests in the parent repo).

## Documentation

Full reference in the [`kx-mcp-server-container`](https://github.com/KxSystems/kx-mcp-server-container)
repository: the [auth guide](https://github.com/KxSystems/kx-mcp-server-container/blob/main/docs/auth.md)
(inbound modes, outbound strategies, authorization) and the design specs (token-exchange,
identity-assertion, authorization). All workspace packages version in lockstep from release tags.
