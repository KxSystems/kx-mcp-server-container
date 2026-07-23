// kdbx_ferry_host.q — real-q backbone for the kdbx "ferry" identity-assertion realidp test.
//
// Trimmed to what the pytest-driven test needs, run non-interactively via
// `q kdbx_ferry_host.q -U <userpass> -p <port>`. Same shape as the live demo host
// (demos/claude-code-live-kdbx/host.q): a service-account qIPC login (-U userpass, matching
// KDBX_DB_USERNAME/PASSWORD the container connects with), the kx.auth module bound to the global
// namespace, a group x table x action grant policy (trader -> read+write trades, default-deny),
// and .s.e wrapped so every SQL query is gated by .kx.auth.authorize[`read;`trades].
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

// --- identity assertion (the kx.auth KDB-X module) ------------------------------------------------
// MUST assign to the global `.kx.auth` so the container's qIPC call `.kx.auth.bind` resolves.
.kx.auth:use`kx.auth;

// The service-account login the container connects as (KDBX_DB_USERNAME) — q's own -U userpass
// file is the connection gate; WHO may then assert an identity is a policy grant below (bind[]
// consults the same policy with (`assert;`identity) keyed on this caller's login, default-deny).
.ferry.svcUser:`kxmcp;

.kx.auth.activate[];

// --- authorization policy (the data-level S/A/R seam) ----------------------------------------------
// Group x table x action grant: the real Keycloak `trader` group may read+write `trades`. Groups
// are extracted by .kx.auth.promote from the asserted principal's claims (top-level `groups` claim,
// the default search order) — the policy keys on membership, not a per-username allow-list.
.ferry.grants:([] grp:`trader`trader; tbl:`trades`trades; act:`read`write);
// Admin grant (the bind[] assert gate): the service-account LOGIN (.z.u, carried as `sub) may
// `assert an `identity. Keyed on the user, not a group.
.ferry.adminGrants:([] usr:enlist .ferry.svcUser; act:enlist `assert; res:enlist `identity);

.ferry.allowed:{[p;a;r]
  acts:$[`read~a; `read`write`delete; enlist a];
  byGroup:0<count select from .ferry.grants      where grp in p`groups, tbl=r, act in acts;
  byUser: 0<count select from .ferry.adminGrants where usr=p`sub, res=r, act=a;
  byGroup or byUser };

.kx.auth.setPolicy[.ferry.allowed];

// Gate every SQL query the kdbx tool runs through the seam (fixed to `read;`trades — the lesson is
// the seam, not SQL parsing, same simplification the demo makes).
.s.realE:.s.e;
.s.e:{[x] .kx.auth.authorize[`read;`trades]; .s.realE x};

-1 "";
-1 "kdbx_ferry_host ready: identity assertion ON, `trader may read+write `trades, default-deny";
