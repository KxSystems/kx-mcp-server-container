/ claude-code-live-kdbx demo host — canonical data + SQL/AI PLUS TWO semantic RBAC sets over kx.auth.
/ .
/ Loads the shippable kx.auth module, logs the service account in via a Tier 0 -U user file, and adds
/ a q-side data gate — then layers on a SECOND, distinct RBAC set so the demo shows the container
/ enforcing TWO semantic layers over ONE .kx.auth engine:
/ .
/   the data gate (PEP-2, q-side)         : .s.e wraps every SQL query with authorize[`read;`trades] —
/                                           a DATA-semantic check (group x table x action) over data grants.
/   the capability check (PEP-1, container): the container calls authorize[`query;`kdbx:sql] BEFORE
/                                           the query — an MCP-semantic check (group x capability) over
/                                           capability grants (KX_MCP_AUTHZ=kdbx_rbac routes it here).
/ .
/ One engine, two callers, two grant SETS, ONE implementation: a single setPolicy fn evaluates one
/ grant table (schema grp;act;res) — "data vs capability" is just which resource a row names (a bare
/ table symbol like `trades vs a colon-namespaced MCP resource like `kdbx:sql), NOT two schemas or two
/ check fns. Grants are chosen to DIVERGE so the two sets are visible:
/   - capability: `viewer`+`trader` may `query` `kdbx:sql   (both alice AND bob may invoke the tool)
/   - data      : `trader` may read `trades`+`instruments and write `trades (alice only)
/ => alice clears both -> rows; bob clears the capability check (may use the SQL tool) but the data
/    gate denies the trades data -> a clean permission_denied. The two-set headline.
/ .
/ Run from the repo root (manual.md drives this):  q demos/claude-code-live-kdbx/host.q -U <userpass>
/ Requires the kx.auth module on the q module path: `just install-modules`.
/ For semantic metadata, install kx.aimeta + its runtime dependencies per manual.md. The standard
/ host loaded below degrades to native introspection when aimeta is unavailable.
/ NB a solitary "/" line would start a block comment — avoided throughout (see the q skill).

/ --- canonical data + SQL/AI interface (reuse the standard host) -------------------------------
system"l examples/host.q";

/ --- identity assertion (the kx.auth KDB-X module) ---------------------------------------------
/ MUST assign to the global `.kx.auth` so the container's qIPC calls (.kx.auth.bind / .authorize) resolve.
.kx.auth:use`kx.auth;
/ Tier 0 service account: the container connects as KDBX_DB_USERNAME/KDBX_DB_PASSWORD. The secret is
/ NOT held here — manual.md starts q with `-U <userpass>` (a standard kdb+ user:md5hash file), which
/ is the connection (password) gate. WHO may then ASSERT an identity is a policy grant (below):
/ .kx.auth.bind consults the same policy with (`assert;`identity) keyed on the caller's login .z.u,
/ so only .demo.svcUser is trusted to assert. activate[] also wires the per-handle .z.po/.z.pc cleanup.
.demo.svcUser:`kxmcp;
/ Groups source: leave kx.auth's DEFAULT search order (it checks the top-level `groups claim first —
/ which is exactly what this realidp Keycloak emits via its group-membership mapper: alice ->
/ `viewer`trader, bob -> `viewer). So policies key on group membership, no setClaims override needed.
.kx.auth.activate[];

/ --- THREE RBAC grant SETS, ONE implementation (the point of this demo) ------------------------
/ Group-keyed grants (the data gate + the capability check) share one schema (grp;act;res); the
/ login-keyed admin grant (WHO may assert an identity) keys on the principal's `sub instead, because
/ the service account is a bare login with no roles. One matching fn evaluates both families — data
/ vs capability vs assert is just which table a row lives in, not a special case.
/ DATA grants (the data gate): the `trader group may read both demo tables and write `trades.
.demo.dataGrants:([] grp:3#`trader; act:`read`read`write; res:`trades`instruments`trades);
/ CAPABILITY grants (the capability check): `viewer and `trader may `query the `kdbx:sql tool capability.
/ Colon-namespaced MCP resources are built with `$"..." — a literal `kdbx:sql would mis-tokenise.
.demo.capGrants:([] grp:`viewer`trader; act:`query`query; res:2#`$"kdbx:sql");
.demo.grants:.demo.dataGrants,.demo.capGrants;
/ ADMIN grant (the bind[] assert gate): the service-account LOGIN (.z.u, carried in `sub) may `assert
/ an `identity. Keyed on the user, not a group — its own small table, matched on `sub.
.demo.adminGrants:([] usr:enlist .demo.svcUser; act:enlist `assert; res:enlist `identity);

/ ONE RBAC implementation: verb subsumption (a write/delete grant satisfies a read check), a no-op
/ for `query — so capability and data evaluate through the SAME check, no per-set branch. satisfiedBy
/ a returns the granted verbs that satisfy a request for `a`. This hand-rolls one degenerate axis of
/ a more general verb-bundle/resource-hierarchy model — a demo simplification, not the full picture.
.demo.satisfiedBy:{[a] $[`read~a; `read`write`delete; enlist a]};
.demo.allowed:{[p;a;r]
  / group-keyed grants (the data gate + the capability check): subject = the asserted principal's `groups
  byGroup:0<count select from .demo.grants      where grp in p`groups, res=r, act in .demo.satisfiedBy a;
  / login-keyed admin grant (the assert gate): subject = the caller's login in `sub
  byUser: 0<count select from .demo.adminGrants where usr=p`sub,      res=r, act=a;
  byGroup or byUser };
.kx.auth.setPolicy[.demo.allowed];

/ --- the data gate: gate every SQL query the kdbx tool runs (q-side, data-semantic) ------------
/ The tool executes via .s.e, so wrapping it gates all SELECT traffic with a DATA check. The
/ capability check (the container-side check) runs earlier, against the same .kx.auth via
/ authorize[`query;`kdbx:sql]. A real policy would derive (action;resource) from the parsed query;
/ the demo fixes (`read;`trades) to keep the lesson on the two seams, not on SQL parsing. This gates
/ the TOOL PATH, not the IPC perimeter (activate[] wires .z.pw/.z.po/.z.pc, not .z.pg) — gating
/ arbitrary q access at the perimeter is a further enhancement, not covered by this demo.
.s.realE:.s.e;
.s.e:{[x] .kx.auth.authorize[`read;`trades]; .s.realE x};

-1 "";
-1 "claude-code-live-kdbx host ready: identity assertion ON, TWO RBAC sets";
-1 "  service account : ",string .demo.svcUser;
-1 "  capability check : groups x capability (default-deny) — `viewer`+`trader may `query `kdbx:sql";
-1 "  data gate        : groups x table x action (default-deny) — `trader may read demo metadata";
