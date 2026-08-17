# `kx.auth` — identity assertion for kdb+

A KDB-X module that lets a kdb+ process act on behalf of an end user whose identity was authenticated
**upstream** — by a trusted gateway, proxy, or application — without that user logging into kdb+
directly.

The trusted caller connects with a service-account login and **asserts** the user's identity by
binding a principal (a dict of the user's claims) to its connection. Permission functions then consult
the bound principal to gate access. kdb+ does no token parsing or crypto: the assertion arrives on an
authenticated service-account connection (ideally over TLS), and `bind` is additionally gated by the
installed policy — the caller's login must hold an `` `assert `` grant on `` `kx.identity `` (see Install).

## Two identities

- **Service account** — the connecting process, authenticated the normal way via `.z.pw`.
- **Asserted principal** — the end user, who never logs in to kdb+. It is bound *after* connect and
  trusted by virtue of the connection it arrives on.

`.z.pw` only ever sees the service account; the principal is asserted, never authenticated, by q.

## Install

KDB-X resolves modules from the runtime's module path (`~/.kx/mod`). Make the module available by
placing — or symlinking — its `kx/auth` directory there:

```bash
cp -r kx/auth ~/.kx/mod/kx/auth          # or: ln -sfn "$PWD/kx/auth" ~/.kx/mod/kx/auth
```

(From the repo root, `just install-modules` does the same.) Then, on the kdb+ host:

```q
.kx.auth:use`kx.auth;                              / bind to a global so a remote `.kx.auth.bind` resolves
.kx.auth.configure[(`svcuser;"service-account-pw")]; / the credentials the trusted caller connects with
.kx.auth.setPolicy[{[p;a;r]                        / bind[] consults this same default-deny policy, so
  (p[`sub]=`svcuser) and (a=`assert) and r=`kx.identity}]; / grant the service account `assert on `kx.identity
.kx.auth.activate[];                               / wire .z.pw / .z.po / .z.pc (composes with any priors)
```

`use` alone is side-effect-free; `activate[]` opts into installing the handlers. The `setPolicy` grant
is required: `bind` consults the same default-deny policy (the caller's login must hold `` `assert ``
on `` `kx.identity ``), so without it every assertion is refused. Full grant-table example:
`demos/claude-code-live-kdbx/host.q`.

`configure` is a self-contained development/reference verifier. Because `activate[]` composes with
the prior `.z.pw`, a host may instead leave `configure` unset and authenticate the service-account
login through its existing `-U` password file or platform `.z.pw`. Use one q-side verifier, not both.
Separately, the MCP process needs one connection-secret source (`KDBX_DB_PASSWORD` or, preferably for
production, `KDBX_DB_PASSWORD_FILE`); it does not read q's password file.

## Reserved resource root

The `kx.*` resource root is reserved for KX module-owned control-plane resources. In particular,
`kx.identity`, `kx.rbac`, and `kx.q` belong to the shared authorization vocabulary; host policies
must not mint application or data resources beneath `kx.*`. Use `data.<table>` for table data and a
product- or application-specific root for other resources. This keeps host-defined grants from
colliding with current or future module contracts.

## Exports

| Export | Signature | Description |
|---|---|---|
| `bind` | `bind[principal]` | After policy permits the caller's login to `assert` on `kx.identity`, promote and bind the principal dict to the current connection handle (`.z.w`). |
| `current` | `current[]` | The principal bound to this handle, or `(::)` when unbound. |
| `valid` | `valid[]` | `1b` iff a principal is bound and unexpired (`exp` arrives as unix seconds and is canonicalised to a q timestamp at bind; absent ⇒ never expires). |
| `require` | `require[]` | Default-deny: signals `'denied` when no valid principal is bound, else returns it. |
| `authorize` | `authorize[action;resource]` | Require a valid principal and enforce the installed Subject/Action/Resource policy; signals `'denied` on refusal. |
| `entitled` | `entitled[action;resources]` | Return the subset of resources the valid principal may access under the installed policy. |
| `setPolicy` | `setPolicy[fn]` | Install the `(principal;action;resource) -> boolean` authorization policy; the default policy denies all. |
| `configure` | `configure[(\`user;"pw")]` | Set the service account `.z.pw` accepts. |
| `setClaims` | `setClaims[paths]` | Configure the dotted claim paths used to promote `groups` and `tenant` into policy-facing fields. |
| `activate` | `activate[]` | Install `.z.pw` (service-account login) and `.z.po`/`.z.pc` (clear the binding on connect/disconnect). |
| `activateHttp` | `activateHttp[]` | Install composed `.z.ph`/`.z.pp` handlers for per-request identity assertion behind a trusted HTTP gateway. |

The principal is an arbitrary claims dict. The module inspects `exp` for validity, promotes configured
claims such as groups and tenant, and checks permissions through `authorize` / `entitled` using the
policy installed by `setPolicy`. Policies can read whatever principal keys they need (e.g. `` `sub ``,
a username, group/tenant claims). When the
caller binds via PyKX (the common case), string claim values arrive as q **symbols**, so compare them
as symbols (don't `` `$ `` them). `bind` canonicalises `exp` from a unix-seconds long to a q
**timestamp**, so it compares directly to `.z.p`.

## Usage — requiring identity and authorizing access

Install a policy that grants the `trader` group `` `read `` on `` `data.trades ``. The compact first
clause retains the trusted service account's required `` `assert `` on `` `kx.identity `` grant from
the Install example; omitting it makes subsequent `bind` calls fail:

```q
allowed:{[p;a;r]
  $[(p[`sub]=`svcuser) and (a=`assert) and r=`kx.identity;
    1b;
    $[`groups in key p;
      (`trader in p`groups) and (a=`read) and r=`data.trades;
      0b]] };

.kx.auth.setPolicy[allowed];
```

Use `require[]` when a function needs a valid asserted identity but has no distinct
action/resource decision:

```q
principalSummary:{[]
  p:.kx.auth.require[];                           / 'denied when unbound or expired
  `sub`groups#p };
```

Use `authorize[action;resource]` for protected operations. It calls `require[]`, evaluates the
installed policy, and returns the valid principal on success, so there is no need to call
`require[]` immediately beforehand:

```q
getTrades:{[s]
  p:.kx.auth.authorize[`read;`data.trades];       / require identity + enforce the S/A/R grant
  / p remains available for audit or finer row-level scoping
  select from trades where sym=s };
```

A principal in the `trader` group may read `` `data.trades ``. The grant resource is deliberately
distinct from the physical q table symbol `trades`; hosts use `data.<table>` for table data and keep
the `kx.*` root for module-owned control-plane resources. A principal without that grant—and any
unbound or expired connection—is refused with a clean `'denied` the caller can handle. Binding an
end user does not give that user the service account's separate assertion privilege.
