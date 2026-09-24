# kx-auth-cli — the `kx auth` CLI

The agent-facing auth CLI for the [KX MCP composition container](https://github.com/KxSystems/kx-mcp-server-container). It ships the `kx`
console script and the `auth` command group: `introspect` (validate a bearer), `login` (acquire one
via the IdP your MCP server advertises), `exchange` (swap a token for a backend-scoped one), and
`assert` (inspect or exercise the kdb+ identity-assertion handshake).

It is distributed and run **where the agent/client runs, not the server** — so it depends only on the
lean [`kx-auth-core`](https://github.com/KxSystems/kx-mcp-server-container/tree/main/packages/kx-auth-core) (the one implementation of bearer validation + the outbound
token-exchange seam, shared with the container) plus `httpx`, and **never on `fastmcp`**. The
client-side install stays light.

> **Built for an agent.** Every subcommand supports `--json` (a structured envelope, never prose) and
> a stable exit-code contract, so an agent can branch on the code without parsing text. Human-readable
> output stays the default for a person at a terminal; add `--json` when something else is calling.

## Table of contents

- [Install](#install)
- [The exit-code contract](#the-exit-code-contract)
- [`kx auth introspect`](#kx-auth-introspect)
- [`kx auth login`](#kx-auth-login)
- [`kx auth exchange`](#kx-auth-exchange)
- [`kx auth assert`](#kx-auth-assert)
- [Token cache](#token-cache)
- [Environment variables](#environment-variables)
- [The fastmcp-free invariant](#the-fastmcp-free-invariant)
- [Related docs](#related-docs)

## Install

Within this workspace, run it straight through `uv`:

```bash
uv run kx auth --help
uv run kx auth introspect --help     # every subcommand has its own --help / -h
```

Standalone (client-side), install the package and call the `kx` script directly (`pipx` / `uv tool`
keep it isolated):

```bash
uv tool install kx-auth-cli          # or: pipx install kx-auth-cli
kx auth --help
```

## The exit-code contract

Stable across every subcommand — branch on the code, not the text:

| Code | Meaning | HTTP analogy |
|---|---|---|
| `0` | ok / allowed | 200 |
| `1` | error — malformed input, unreachable endpoint, unexpected failure | 400/500 |
| `2` | usage — bad flags/args, or a missing required input (argparse's own exit) | — |
| `3` | auth-required — no/expired credential; the agent should `login` | 401 |
| `4` | denied — valid credential, authorization refused | 403 |

`--json` emits `{"status": …, …}` on success and `{"status": …, "reason": …}` on failure.

## `kx auth introspect`

Validate a bearer against the **same keys the container enforces** (the shared `kx-auth-core`
verifier). It's stateless, so it never touches the token cache, and it takes its token from the
first source found: `argument` → `$KX_AUTH_TOKEN` → stdin.

```bash
# token + key as flags, structured verdict
kx auth introspect "$TOKEN" --public-key-path key.pub \
  --issuer https://idp --audience my-api --json

# config + token entirely from the environment (matches the container)
KX_MCP_AUTH=static KX_MCP_AUTH_PUBLIC_KEY_PATH=key.pub \
  KX_AUTH_TOKEN="$TOKEN" kx auth introspect --json

# validate against a live JWKS endpoint, token piped on stdin
echo "$TOKEN" | kx auth introspect --jwks-uri https://idp/.well-known/jwks.json
```

Verification config defaults from the `KX_MCP_AUTH*` environment (the same vars the container reads);
the flags (`--jwks-uri` / `--public-key[-path]` / `--issuer` / `--audience` / `--algorithm` /
`--required-scopes`) override per call.

## `kx auth login`

Acquire a bearer for an MCP server via the **device-code** flow, against the authorization server the
server advertises — you never hand-configure a realm, client-id, or AS URL.

- `--server` is the **MCP-server (resource) URL you connect to — not a backend.** The container
  publishes [RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728) Protected Resource Metadata
  naming its authorization server; `kx auth` discovers the AS and runs the
  [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628) device-code flow **directly against it** (the
  spec forbids the resource server passing the token through). The acquired token is audienced to the
  MCP-server resource; backend-scoped tokens come from a separate [`exchange`](#kx-auth-exchange).
- **Client identity is DCR-first:** when the AS advertises a `registration_endpoint`, the client is
  registered dynamically ([RFC 7591](https://www.rfc-editor.org/rfc/rfc7591)). `--client-id` /
  `$KX_AUTH_CLIENT_ID` overrides.
- The acquired credential is written to the [token cache](#token-cache), so `exchange` can chain off it.

```bash
kx auth login --server https://mcp.example --json
kx auth login --server https://mcp.example \
  --client-id my-public-client --scope "kdbx.read offline_access"
```

The verification prompt (the URL + user code to approve) is printed to **stderr**, so a `--json`
result on stdout stays clean.

Exit codes: `0` cached · `1` discovery/network/no-device-support/DCR failure · `3` the device code
expired or timed out before approval · `4` the user denied the request.

## `kx auth exchange`

Swap a subject token for a backend-scoped credential — a thin wrapper over the **shared
`kx_auth_core.exchange` seam** (the same outbound code path the container's backends use, so the wire
shape and audit chain are one implementation). This is the workload-identity bootstrap target
(`kubectl create token` / `gh actions-token` → `kx auth exchange` → backend-scoped token).

Default strategy is `rfc_8693`; `passthrough` and `service_account` (and custom registered strategies)
are selectable with `--strategy`. Subject source (first found wins): `--subject` → `$KX_AUTH_TOKEN` →
stdin → **the `login` cache for `--server`** (the seamless `login` → `exchange` chain).
`service_account` needs no subject.

```bash
# RFC 8693 swap (subject as a flag), structured output
kx auth exchange --subject "$TOK" --audience kdbai \
  --token-url https://idp/token --client-id mcp-container --client-secret "$SECRET" --json

# chain off a prior login — subject pulled from the cache for that server
kx auth login --server https://mcp.example --json
kx auth exchange --server https://mcp.example --audience backend-api --token-url https://idp/token --json

# workload-identity bootstrap (token from the platform, on stdin)
kubectl create token my-sa | kx auth exchange --audience kdbai --token-url https://idp/token --json
```

Exit codes: `0` minted/forwarded · `1` misconfig / unreachable endpoint / no token returned · `2` no
subject for a strategy that needs one · `3` the cached login for `--server` has expired (run
`kx auth login` again) · `4` the strategy refused (e.g. a `passthrough` audience-guard mismatch).

## `kx auth assert`

Inspect the identity projection that the container would send to plain kdb+, or exercise the full
identity-assertion handshake against a live q process. Without `--connect`, the command projects
claims into the shared principal wire format and prints the result; this mode needs neither kdb+ nor
PyKX. With `--connect HOST:PORT`, it logs in over qIPC as the service account, calls
`.kx.auth.bind`, checks `.kx.auth.valid[]`, and optionally runs a q expression supplied with
`--probe`.

Claims source (first found wins): `--principal` JSON (`-` for stdin or `@file` for a file) →
`--token` → `$KX_AUTH_TOKEN`. JWT claims are decoded **without verification**: `assert` is a local
projection/handshake diagnostic, not a replacement for [`introspect`](#kx-auth-introspect).

```bash
# project-only: inspect exactly what the container would bind
kx auth assert --principal '{"sub":"alice","scope":"kdbx.read","aud":"kx-mcp"}' --json

# bind and verify against a live q process (requires kx-auth-cli[qipc])
kx auth assert --token "$TOK" --connect localhost:5010 \
  --user kxmcp --password "$SVC_PW" --probe "select from trades" --json
```

The live handshake requires the optional qIPC dependency (`pip install 'kx-auth-cli[qipc]'`). Prefer
`$KX_AUTH_KDB_PASSWORD` over `--password` so the service-account password does not land in shell
history or process listings; `$KX_AUTH_KDB_USER` supplies the user when `--user` is omitted.

Exit codes: `0` projected or bound successfully · `1` invalid claims / connection / module error ·
`2` missing input or invalid `--connect` value · `4` the optional probe was denied by q-side policy.

## Token cache

`login` writes a single JSON credential file, read by `exchange`:

- **Path:** `~/.kx/credentials.json`, overridable via `$KX_AUTH_CACHE`. Mode `0600`.
- **Keyed by** the `--server` (MCP-server / resource) URL, so multiple deployments coexist.
- Each entry holds `access_token` / `refresh_token` / `token_type` / `scope` / `expires_at` /
  `issued_at` / `authorization_server`.
- `exchange` refuses an entry whose `expires_at` has passed (exit `3` — re-`login`) rather than
  sending a dead token to the STS.
- `introspect` is stateless and never touches it. OS-keyring storage is a later hardening.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `KX_AUTH_TOKEN` | `introspect`, `exchange`, `assert` | The bearer / subject token when not passed as an argument. `assert` decodes its claims without verification. |
| `KX_AUTH_CLIENT_ID` | `login` | OAuth client id when not registering dynamically (`--client-id` overrides). |
| `KX_AUTH_CLIENT_SECRET` | `exchange` | Client secret when not passed as `--client-secret` — preferred, since a flag lands in shell history and process listings. |
| `KX_AUTH_CACHE` | `login`, `exchange` | Override the token-cache path (default `~/.kx/credentials.json`). |
| `KX_AUTH_KDB_USER` | `assert --connect` | Service-account user for the qIPC login when `--user` is omitted. |
| `KX_AUTH_KDB_PASSWORD` | `assert --connect` | Service-account password for the qIPC login when `--password` is omitted. |
| `KX_MCP_AUTH*` | `introspect` | The verification config the container reads (`KX_MCP_AUTH`, `KX_MCP_AUTH_PUBLIC_KEY[_PATH]`, `KX_MCP_AUTH_JWKS_URI`, `KX_MCP_AUTH_ISSUER`, `KX_MCP_AUTH_AUDIENCE`, `KX_MCP_AUTH_ALGORITHM`, `KX_MCP_AUTH_REQUIRED_SCOPES`). |

## The fastmcp-free invariant

This CLI runs client-side and must ship light, so it depends on `kx-auth-core` + `httpx` only and
**must never import `fastmcp`**. Two things hold the line:

- `introspect`'s verifier is the shared `kx_auth_core.verify_token`, kept **in agreement with the
  container's `JWTVerifier`** by `tests/deterministic/unit/test_auth_cli_contract.py`.
- `login`'s discovery / device-code / token-cache machinery is **CLI-local, not in `kx-auth-core`** —
  the container never logs in and the cache is a client-side concern, which keeps the shared core's
  charter (mechanisms reused by container + bundles + CLI) clean.
- `assert` uses the shared, fastmcp-free identity projection. PyKX is imported lazily only for the
  optional `--connect` path and is distributed through the `qipc` extra, not the base CLI.

## Related docs

The user-facing auth reference is the [auth guide](https://github.com/KxSystems/kx-mcp-server-container/blob/main/docs/auth.md)
in the [`kx-mcp-server-container`](https://github.com/KxSystems/kx-mcp-server-container) repository.
