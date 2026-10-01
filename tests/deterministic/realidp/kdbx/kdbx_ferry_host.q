// kdbx_ferry_host.q — real-q backbone for the kdbx "ferry" identity-assertion realidp test.
//
// Trimmed to what the pytest-driven test needs, run non-interactively via
// `q kdbx_ferry_host.q -U <userpass> -p <port>`. Same shape as the live demo host
// (demos/claude-code-live-kdbx/host.q): a service-account qIPC login (-U userpass, matching
// KDBX_DB_USERNAME/PASSWORD the container connects with), the kx.auth module bound to the global
// namespace, a group x resource x action grant policy (trader -> read+write data.trades, default-deny),
// and .s.e wrapped so every SQL query is gated by .kx.auth.authorize[`read;`data.trades].
//
// The test reuses the REAL live Keycloak `quants` realm's existing `trader` group
// (singular — alice has it, bob does not) and its groups-mapper, which already puts group
// membership in a top-level `groups` claim — so no .kx.auth.setClaims call is needed here.
//
// NB a solitary "/" line opens a block comment that silently voids the rest of the file (the q
// skill's gotcha) — this file uses "//" throughout to avoid it entirely.
//
// NB does NOT `system"l examples/host.q"` (unlike the demo). `.s.init[]` is the SQL initialization
// contract; inlining the same trades-table seed examples/host.q uses keeps this fixture focused
// and self-contained.

// --- canonical data + SQL interface ---------------------------------------------------------------
.s.init[];

trades:([]
  time : 2024.01.02D09:30:00.000000000 + 1000000000 * til 10;
  sym  : 10#`AAPL`MSFT`GOOG`AMZN`NVDA;
  side : 10#`B`S;
  price: 187.45 411.22 142.18 155.03 720.91 188.10 410.85 142.55 154.60 722.34;
  size : 100 250 75 500 40 120 300 60 450 35
 );

// --- identity assertion + the peer policy engine (the kx.auth / kx.rbac KDB-X modules) -----------
// MUST assign to the global `.kx.auth` so the container's qIPC call `.kx.auth.bind` resolves.
// Both peers are resolved from the q module path; they are canonical in the kx-auth repo and are NOT
// vendored here — `just install-modules` puts them on $QPATH.
.kx.auth:use`kx.auth;
.kx.rbac:use`kx.rbac;

// The service-account login the container connects as (KDBX_DB_USERNAME) — q's own -U userpass
// file is the connection gate; WHO may then assert an identity is a policy grant below (bind[]
// consults the same policy with (`assert;`kx.identity) keyed on this caller's login, default-deny).
.ferry.svcUser:`kxmcp;

.kx.auth.activate[];

// --- login groups: the service account's own identity ---------------------------------------------
// A kdb+ login carries no IdP groups, which is the only reason the assert grant ever needed its own
// `usr`-keyed table — a policy-layer patch for an identity-layer gap. The module's login->groups map
// closes it, so the assert grant is an ordinary group-keyed row. An unmapped login matches nothing.
.kx.auth.setLoginGroups[(enlist .ferry.svcUser)!enlist `superUsers];

// --- authorization policy: ONE grant table, ONE engine (the peer kx.rbac module) -------------------
// No host-written decision function: kx.rbac decides. Groups stay the principal key — .kx.auth.promote
// extracts them from the real Keycloak principal's top-level `groups` claim (the default search
// order) — so the policy keys on membership, not a per-username allow-list.
//
// NB the hand-rolled policy this replaces made a write/delete grant IMPLY read. kx.rbac has no verb
// subsumption by design (pinned absent by its own `noVerbSubsumption` regression). Not a behaviour
// change here: both verbs are granted explicitly below, so every decision is unchanged.
.kx.rbac.grant[`trader; `read;  `data.trades];
.kx.rbac.grant[`trader; `write; `data.trades];
// ASSERT grant (the bind[] gate): the service account's tier may assert an identity.
.kx.rbac.grant[`superUsers; `assert; `kx.identity];

// Install the peer engine's scalar decision function — explicit, so a bare `use` changes nothing.
.kx.auth.setPolicy .kx.rbac.policy[];

// Gate every SQL query the kdbx tool runs through the seam (fixed to `read;`data.trades — the lesson is
// the seam, not SQL parsing, same simplification the demo makes).
.s.realE:.s.e;
.s.e:{[x] .kx.auth.authorize[`read;`data.trades]; .s.realE x};

-1 "";
-1 "kdbx_ferry_host ready: identity assertion ON, kx.rbac installed — ",(string count .kx.rbac.grants[])," grants";
-1 "  `trader may read+write `data.trades; `superUsers may `assert `kx.identity; default-deny";
