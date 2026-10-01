/ claude-code-live-kdbx demo host — canonical data + SQL/AI PLUS TWO semantic RBAC sets over kx.auth.
/ .
/ Loads the kx.auth module and its peer policy engine kx.rbac, logs the service account in via a
/ Tier 0 -U user file, and adds a q-side data gate — then layers on a SECOND, distinct RBAC set so the
/ demo shows the container enforcing TWO semantic layers over ONE policy engine:
/ .
/   the data gate (PEP-2, q-side)         : .s.e wraps every SQL query with authorize[`read;`data.trades] —
/                                           a DATA-semantic check (group x resource x action) over data grants.
/   the capability check (PEP-1, container): the container calls authorize[`query;`kdbx.sql] BEFORE
/                                           the query — an MCP-semantic check (group x capability) over
/                                           capability grants (KX_MCP_AUTHZ=kdbx_rbac routes it here).
/ .
/ One engine, two callers, two grant SETS, ONE implementation — and the implementation is no longer
/ hand-written: kx.rbac evaluates one grant table (schema grp;act;res), so "data vs capability" is just
/ which resource a row names (a data resource like `data.trades vs a capability resource like
/ `kdbx.sql), NOT two schemas or two check fns. Grants are chosen to DIVERGE so the two sets are visible:
/   - capability: `viewer`+`trader` may `query` `kdbx.sql   (both alice AND bob may invoke the tool)
/   - data      : `trader` may read `data.trades`+`data.instruments and write `data.trades (alice only)
/ => alice clears both -> rows; bob clears the capability check (may use the SQL tool) but the data
/    gate denies the trades data -> a clean permission_denied. The two-set headline.
/ .
/ Run from the repo root (manual.md drives this):  q demos/claude-code-live-kdbx/host.q -U <userpass>
/ Requires kx.auth + kx.rbac on the q module path: `just install-modules`. Both live in the kx-auth
/ repo (KxSystems/kx-auth), NOT in this one.
/ For semantic metadata, install kx.aimeta + its runtime dependencies per manual.md. The standard
/ host loaded below degrades to native introspection when aimeta is unavailable.
/ NB a solitary "/" line would start a block comment — avoided throughout (see the q skill).

/ --- canonical data + SQL/AI interface (reuse the standard host) -------------------------------
system"l examples/host.q";

/ --- identity assertion (the kx.auth KDB-X module) ---------------------------------------------
/ MUST assign to the global `.kx.auth` so the container's qIPC calls (.kx.auth.bind / .authorize) resolve.
.kx.auth:use`kx.auth;
.kx.rbac:use`kx.rbac;
/ Tier 0 service account: the container connects as KDBX_DB_USERNAME/KDBX_DB_PASSWORD. The secret is
/ NOT held here — manual.md starts q with `-U <userpass>` (a standard kdb+ user:md5hash file), which
/ is the connection (password) gate. WHO may then ASSERT an identity is a policy grant (below):
/ .kx.auth.bind consults the same policy with (`assert;`kx.identity) keyed on the caller's login .z.u,
/ so only .demo.svcUser is trusted to assert. activate[] also wires the per-handle .z.po/.z.pc cleanup.
.demo.svcUser:`kxmcp;
/ Groups source: leave kx.auth's DEFAULT search order (it checks the top-level `groups claim first —
/ which is exactly what this realidp Keycloak emits via its group-membership mapper: alice ->
/ `viewer`trader, bob -> `viewer). So policies key on group membership, no setClaims override needed.
.kx.auth.activate[];

/ --- login groups: the service account's own identity ------------------------------------------
/ A kdb+ login carries no IdP groups, which is the ONLY reason the assert grant ever needed its own
/ `usr`-keyed table — that was a policy-layer patch for an identity-layer gap. The module's
/ login->groups map closes it, so the assert grant is an ordinary group-keyed row and all three sets
/ below live in ONE table with ONE schema. An unmapped login has no groups and matches nothing.
.kx.auth.setLoginGroups[(enlist .demo.svcUser)!enlist `superUsers];

/ --- THREE RBAC grant SETS, ONE TABLE, ONE ENGINE (the point of this demo) ---------------------
/ There is no host-written decision function any more: kx.rbac decides, and "data vs capability vs
/ assert" is only WHICH RESOURCE a row names. Declared through the engine's own admin verbs, so these
/ grant[] calls at load time ARE the reviewable, version-controlled baseline.
/ .
/ DATA grants (the data gate): the `trader group may read both demo resources and write `data.trades.
.kx.rbac.grant[`trader; `read;  `data.trades];
.kx.rbac.grant[`trader; `read;  `data.instruments];
.kx.rbac.grant[`trader; `write; `data.trades];
/ CAPABILITY grants (the container-side check): `viewer and `trader may `query the `kdbx.sql capability.
.kx.rbac.grant[`viewer; `query; `kdbx.sql];
.kx.rbac.grant[`trader; `query; `kdbx.sql];
/ ASSERT grant (the bind[] gate): the service account's tier may assert an identity. WHO may assert is
/ just another grant, not a separate seam.
.kx.rbac.grant[`superUsers; `assert; `kx.identity];

/ The hand-rolled policy this replaces implemented verb subsumption (a write/delete grant satisfying a
/ read check). kx.rbac deliberately has none — the verb set is tiny and closed, so exact match plus the
/ null wildcard is the whole mechanism, and the old `satisfiedBy` hand-rolled one degenerate axis of a
/ model the engine declined to build. No behaviour changes here: every verb the demo checks is granted
/ explicitly above. What the engine ADDS is segment-prefix resource cover — a grant on `data would now
/ cover `data.trades — which this demo does not use but a host should know it has.
.kx.auth.setPolicy .kx.rbac.policy[];

/ --- the data gate: gate every SQL query the kdbx tool runs (q-side, data-semantic) ------------
/ The tool executes via .s.e, so wrapping it gates all SELECT traffic with a DATA check. The
/ capability check (the container-side check) runs earlier, against the same .kx.auth via
/ authorize[`query;`kdbx.sql]. A real policy would derive (action;resource) from the parsed query;
/ the demo fixes (`read;`data.trades) to keep the lesson on the two seams, not on SQL parsing. This gates
/ the TOOL PATH, not the IPC perimeter (activate[] wires .z.pw/.z.po/.z.pc, not .z.pg) — gating
/ arbitrary q access at the perimeter is a further enhancement, not covered by this demo.
.s.realE:.s.e;
.s.e:{[x] .kx.auth.authorize[`read;`data.trades]; .s.realE x};

-1 "";
-1 "claude-code-live-kdbx host ready: identity assertion ON, THREE RBAC sets over ONE kx.rbac engine";
-1 "  service account : ",string .demo.svcUser;
-1 "  policy engine    : kx.rbac, installed — ",(string count .kx.rbac.grants[])," grants declared via grant[]";
-1 "  assert gate      : `superUsers may `assert `kx.identity, reached by the ",string[.demo.svcUser]," login";
-1 "  capability check : groups x capability (default-deny) — `viewer`+`trader may `query `kdbx.sql";
-1 "  data gate        : groups x resource x action (default-deny) — `trader may read demo metadata";
