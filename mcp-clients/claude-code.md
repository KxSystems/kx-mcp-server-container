# Claude Code Configuration Guide

How to connect [Claude Code](https://claude.com/claude-code) to the KX MCP composition container.
Claude Code registers MCP servers with `claude mcp add`; this guide covers both the **no-auth**
(local bundling) and **inbound-auth** (shared HTTP server) postures.

## Prerequisites

- **Claude Code installed** (`claude` on your PATH).
- The container set up (`uv sync`) — see the [main README](../README.md#quickstart).

## No-auth: local STDIO bundling

The single-principal posture — Claude Code spawns the container as a STDIO child and owns its
lifecycle. Identity collapses to the developer who launched it, so there is no auth to configure
(see [Zero-config STDIO bundling](../README.md#zero-config-stdio-bundling)):

```bash
claude mcp add kx-mcp -- \
  uv --directory /absolute/path/to/kx-mcp-server-container run kx-mcp --bundles kdbx --transport stdio
```

`claude mcp list` should then show `kx-mcp` connected, and `/mcp` in a session lists its tools.

## Inbound auth: shared HTTP server

When the container runs as a jwks-protected HTTP server (see
[Running with inbound auth](../README.md#running-with-inbound-auth)), Claude Code connects over the
`http` transport and must present a valid bearer.

### Native discovery (recommended)

If the server is launched with `KX_MCP_AUTH_RESOURCE_URL` set, it advertises its authorization server
via [RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728) Protected Resource Metadata. Register it
with **no token** — Claude Code discovers the login itself:

```bash
claude mcp add --transport http kx-mcp http://127.0.0.1:8000/mcp
# -s user to share the registration across projects; default scope is "local"
```

On first use, `/mcp` triggers Claude Code's built-in OAuth: it reads the `401`'s `resource_metadata`
pointer, fetches the metadata, runs a browser flow **directly against the IdP**, and stores +
auto-refreshes the token — no header to manage.

If the IdP issues tokens whose `aud` doesn't match the server's `KX_MCP_AUTH_AUDIENCE`, pin the client
that does: `claude mcp add --transport http kx-mcp http://127.0.0.1:8000/mcp --client-id <client>`.

> **Caveat — the IdP must serve its metadata where the client looks.** The container advertises the
> issuer as the authorization server; Claude Code then fetches that issuer's authorization-server
> metadata. For an issuer with a path (e.g. Keycloak `…/realms/<realm>`), clients build a
> [RFC 8414](https://datatracker.ietf.org/doc/html/rfc8414) **host-inserted** URL
> (`https://host/.well-known/oauth-authorization-server/realms/<realm>`). If a gateway intercepts
> root-level paths and returns an HTML login instead of authorization-server metadata, native
> discovery fails even though the container is correct. The gateway should route
> `/.well-known/oauth-authorization-server/*` to the IdP or return 404. Until then, use the header
> fallback below.

### Fallback: supply the bearer via a header

If the server doesn't advertise discovery (`KX_MCP_AUTH_RESOURCE_URL` unset — e.g. an older deployment)
or you already hold a token, inject it as a header. It is captured at add-time and is **static**
(Claude Code will not refresh it), so re-add when it expires:

```bash
claude mcp add --transport http kx-mcp http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <your-token>"
```

## The `kx auth` CLI

The [`kx auth`](../packages/kx-auth-cli/README.md) CLI is the agent-facing auth surface — `login`
(device-code, discovery-driven), `introspect`, and `exchange`. `login` uses the **same** server-
advertised RFC 9728 discovery as Claude Code (`kx auth login --server <mcp-url>`), so it needs the
server launched with `KX_MCP_AUTH_RESOURCE_URL` too; it differs only in flow (device-code vs Claude
Code's browser auth-code). `introspect` and `exchange` work against any bearer you already hold.

## Troubleshooting

- **`kx-mcp` not connected in `claude mcp list`** — confirm the server is running and reachable
  (`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/mcp` → `401` means up and
  auth-guarded), and that the header token hasn't expired.
- **Calls start failing after a few minutes** — the injected bearer expired; re-mint and re-add.
- **`401` on every call** — the token's `iss`/`aud` must match the server's `KX_MCP_AUTH_ISSUER` /
  `KX_MCP_AUTH_AUDIENCE`. Validate it with `uv run kx auth introspect <token> --json`.

## Additional resources

- [Claude Desktop configuration](claude-desktop.md)
- [Main README](../README.md) · [Running with inbound auth](../README.md#running-with-inbound-auth)
- [Official Claude Code MCP docs](https://docs.claude.com/en/docs/claude-code/mcp)
