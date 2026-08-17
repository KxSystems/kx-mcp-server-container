// kdbx_data_gate_host.q — real-q backbone for the PEP-2 data-gate end-to-end regression.
//
// Run non-interactively via `q kdbx_data_gate_host.q -U <userpass> -p <port>`. Same canonical-data
// shape as kdbx_ferry_host.q (the realidp ferry lane) and the live demo host, but deliberately does
// NOT wrap `.s.e` — that's the OTHER, q-side-SQL-gate pattern the ferry host demonstrates. This host
// exercises the KDBX_DB_DATA_GATE mechanism instead: the container's Python tools consult
// `.kx.auth.entitled[action;resources]` explicitly (via consult_data_gate), BEFORE running any query
// — so gating happens container-side, and `.s.e` here stays the plain, ungated SQL interface.
//
// No live IdP needed: KX_MCP_AUTH=static mints local tokens with whatever `sub`/`groups` claims a
// test wants, so this lives in the deterministic integration tier, not realidp.
//
// NB a solitary "/" line opens a block comment that silently voids the rest of the file (the q
// skill's gotcha) — this file uses "//" throughout to avoid it entirely.

// --- canonical data + SQL interface ---------------------------------------------------------------
.s.init[];

trades:([]
  time : 2024.01.02D09:30:00.000000000 + 1000000000 * til 10;
  sym  : 10#`AAPL`MSFT`GOOG`AMZN`NVDA;
  side : 10#`B`S;
  price: 187.45 411.22 142.18 155.03 720.91 188.10 410.85 142.55 154.60 722.34;
  size : 100 250 75 500 40 120 300 60 450 35
 );

// A second real table so partial/none-entitled personas have something to be DENIED — `derive_tables`
// and the metadata resource's `live_table_names` both need an actual q table, not just a policy-known
// name, so entitled[]'s scope-down has a real second table to refuse.
secrets:([]
  id   : 1 2 3;
  memo : ("payroll band review"; "M&A due diligence notes"; "incident postmortem — unreleased")
 );

// --- identity assertion (the kx.auth KDB-X module) ------------------------------------------------
// MUST assign to the global `.kx.auth` so the container's qIPC call `.kx.auth.bind` resolves.
.kx.auth:use`kx.auth;

// The service-account login the container connects as (KDBX_DB_USERNAME/PASSWORD). kx.auth's OWN
// .z.pw wiring is the connection gate here (not q's built-in -u/-U) — defining .z.pw (which
// activate[] does) makes q consult it on every connection attempt regardless of -u/-U, and
// pwCheck compares this exact credential once configured. WHO may then assert an identity is a
// SEPARATE policy grant below (bind[] consults the same policy with (`assert;`kx.identity) keyed on
// this caller's login, default-deny) — two gates, connection-level then assertion-level.
.gate.svcUser:`kxmcp;
.gate.svcPassword:"kdbx-data-gate-test-svc-pw";

.kx.auth.configure[(.gate.svcUser; .gate.svcPassword)];
.kx.auth.activate[];

// --- authorization policy (the data-level S/A/R seam, consulted by entitled[]) ---------------------
// Group x resource x action grant: `trader` may read `data.trades`; nobody is granted `data.secrets` — proves the
// none-entitled and partial-entitled (scope-down) cases with one real second table.
.gate.grants:([] grp:enlist`trader; res:enlist`data.trades; act:enlist`read);
// Admin grant (the bind[] assert gate): the service-account LOGIN (.z.u, carried as `sub) may
// `assert on `kx.identity. Keyed on the user, not a group.
.gate.adminGrants:([] usr:enlist .gate.svcUser; act:enlist`assert; res:enlist`kx.identity);

.gate.allowed:{[p;a;r]
  byGroup:0<count select from .gate.grants      where grp in p`groups, res=r, act=a;
  byUser: 0<count select from .gate.adminGrants where usr=p`sub, res=r, act=a;
  byGroup or byUser };

.kx.auth.setPolicy[.gate.allowed];

-1 "";
-1 "kdbx_data_gate_host ready: identity assertion ON, `trader may read `data.trades, `data.secrets ungranted";
