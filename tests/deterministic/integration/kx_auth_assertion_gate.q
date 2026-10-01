/ Regression for the kx.auth identity-assertion gate: bind[] asks the SAME authorization policy WHO
/ may assert — an S/A/R decision keyed on the caller's login (.z.u, passed as `sub`) with action
/ `assert on resource `kx.identity, default-deny (policy is deny-all until setPolicy). "Who may assert"
/ is a host grant in the one policy, not a module built-in. Exits 0 on ALL PASS / 1 on any failure so
/ the pytest wrapper can assert on the return code. Run directly: `q <this file>` (needs a license).
/ .
/ This covers the module LOGIC; the live .z.u-over-IPC property (a real second connection under a
/ different login is refused at bind) is exercised by a live demo / the kdb-x realidp lane. In-process
/ .z.u is fixed, so we grant `assert to whatever .z.u is and prove caller-keying by also granting it
/ to a DIFFERENT login and confirming bind is then refused.
/ .
/ Also covers the rest of the module's export surface not exercised elsewhere: entitled[] (the PEP-2
/ data-gate's scope-down verb — full/partial/none/empty-input, and its own require[] gate), configure[]
/ (the malformed-arg guard, and the delegate-before/enforce-after pwCheck behaviour), and the HTTP path
/ (fromJson[]'s promotion, serveHttp[]'s per-request binding + case-insensitive header match + always-
/ clears-on-error semantics, and activateHttp[]'s composition with a prior .z.ph). `bind`/`current`/
/ `authorize`/`setPolicy` are covered above; `setClaims`/`valid` have their own real-q regression in
/ kx_auth_rebind.q; `require` is exercised transitively throughout (both entitled[] and authorize[]
/ route through it).
/ The module is NOT vendored in this repo — it is canonical in kx-auth and resolved onto the q module
/ path by `just install-modules`. Load it FLAT rather than with `use`: this regression asserts on
/ PRIVATE state (promote / fromJson / serveHttp are not in the export dict), which `use` cannot reach.
/ $KX_AUTH_MOD overrides the module root when working from a checkout at a non-standard path.
/ QPATH may be a ":"-separated list, so take the first entry (then ~/.kx/mod) that actually holds kx/auth.
modRoot:{[] p:getenv`KX_AUTH_MOD; if[count p;:p];
  d:":" vs getenv`QPATH; c:((d where 0<count each d),enlist getenv[`HOME],"/.kx/mod"),\:"/kx";
  h:c where {not ()~key hsym `$x,"/auth/init.q"} each c; $[count h; first h; last c]}[];
if[()~key hsym `$modRoot,"/auth/init.q"; '"no kx.auth at ",modRoot," — run `just install-modules` (or set KX_AUTH_MOD)"];
system"l ",modRoot,"/auth/init.q";

u:.z.u;   / the in-process caller login — what bind passes to the policy as the subject's `sub`

/ 1. default-deny: policy unset (deny-all) -> bind refuses (a mere connection is not enough).
e1:@[{bind[()!()];`ok};(::);{x}];
r1:$[10h=type e1; "denied"~6#e1; 0b];

/ 2. a policy granting `assert on `kx.identity to THIS caller -> bind succeeds; current[] reflects the
/    ASSERTED principal (whose `sub differs from the caller — proving caller != asserted subject).
setPolicy[{[p;a;r] (a~`assert) and (r~`kx.identity) and p[`sub]~u}];
bind[`sub`groups!(`enduser;`viewer`trader)];
r2:`enduser~(current[])`sub;

/ 3. grant `assert only to a DIFFERENT login -> bind refuses (proves it keys on the caller .z.u).
setPolicy[{[p;a;r] (a~`assert) and (r~`kx.identity) and p[`sub]~`otheruser}];
e3:@[{bind[()!()];`ok};(::);{x}];
r3:$[10h=type e3; "denied"~6#e3; 0b];

/ 4. the data S/A/R path is unaffected: assert allowed, `read`data.trades granted to `trader, others denied.
setPolicy[{[p;a;r] $[(a~`assert) and r~`kx.identity; 1b; (a~`read) and (r~`data.trades) and `trader in p`groups]}];
bind[`sub`groups!(`alice;enlist`trader)];                    / enlist: mirror PyKX's 1-group vector shape
r4:`trader in (authorize[`read;`data.trades])`groups;        / granted -> returns the principal
e5:@[{authorize[`read;`data.secrets];`ok};(::);{x}];
r5:$[10h=type e5; "denied"~6#e5; 0b];                        / ungranted -> 'denied (default-deny governs)

/ ---- the rest of the export surface: entitled[], configure[], and the HTTP path -------------------
/ Same file/lane (needs a license, self-skips without one); extends the assert-gate coverage above
/ rather than standing up a second q fixture.

/ 6-10. entitled[]: the scope-down companion to authorize[] — the allowed subset of a resource LIST
/    in one round-trip. This is what the kdb-x data-gate adapter consults (PEP-2); it must return a
/    subset (never signal) on partial/no entitlement, but still enforce require[]'s default-deny gate
/    when NO principal is bound at all.
setPolicy[{[p;a;r] $[(a~`assert) and r~`kx.identity; 1b; (a~`read) and (r in `data.trades`data.orders) and `trader in p`groups]}];
bind[`sub`groups!(`alice;enlist`trader)];
r6:(`data.trades`data.orders)~entitled[`read;`data.trades`data.orders]; / all entitled -> full vector
r7:(enlist`data.trades)~entitled[`read;`data.trades`data.secrets]; / partial -> strict entitled subset
r8:0=count entitled[`read;enlist`data.secrets];                / none entitled -> EMPTY vector, not a signal
r9:0=count entitled[`read;`symbol$()];                          / empty input short-circuits, no policy consult
/ 10. THE SUBJECT RULE (amends M4's unbound-handle contract). An unbound handle no longer FAILS: it
/    falls back to the connecting login's own principal, which is default-deny until mapped through
/    setLoginGroups. So the guarantee moved rather than weakened — access is still refused, but by the
/    POLICY naming the login rather than by require[]'s "no principal" gate. Pin all three halves, or a
/    regression could restore the old signal (or, far worse, grant the fallback something).
/    Unbind through the module's own `clear` verb: reassigning the private `bound` store from top-level
/    script scope leaves it in a state where the next global read signals 'type.
clear .z.w;
p10:current[];                                                  / resolves — no signal — to the LOGIN
c10a:(.z.u~p10`sub) and (0=count p10`groups) and (`kdb.local~p10`iss);
c10b:0=count entitled[`read;enlist`data.trades];                / unmapped login entitles nothing
e10:@[{authorize[`read;`data.trades];`ok};(::);{x}];
c10c:$[10h=type e10; "denied"~6#e10; 0b];                       / ... and authorize still signals 'denied
r10:c10a and c10b and c10c;
bind[`sub`groups!(`alice;enlist`trader)];                       / rebind for the checks below

/ 11. configure[]: rejects a malformed arg; a good (`user;"pw") arg makes pwCheck ENFORCE it — before
/    configure[], svc is unset and pwCheck DELEGATES to the (permissive-by-default) prior handler.
e11:@[{configure[enlist`onlyone];`ok};(::);{x}];
c11a:$[10h=type e11; "kx.auth.configure"~17#e11; 0b];
c11b:pwCheck[`anyone;"anything"];                                / pre-configure: delegates -> permissive
configure[(`svcuser;"svcpass")];
c11c:pwCheck[`svcuser;"svcpass"];                                / post-configure: the exact credential passes
c11d:(not pwCheck[`svcuser;"wrongpass"]) and not pwCheck[`otheruser;"svcpass"];
r11:c11a and c11b and c11c and c11d;

/ 12. fromJson[]: the HTTP-path principal runs through the SAME promote[] qIPC's bind[] uses.
p12:fromJson "{\"sub\":\"carol\",\"groups\":[\"trader\"]}";
r12:(`carol~p12`sub) and ((enlist`trader)~p12`groups);

/ 13. serveHttp[]: binds the header-carried principal for ONE request only (reqPrincipal), matched
/    case-insensitively, and ALWAYS clears it after — including when the wrapped handler raises.
/    Compared against `before` (the AMBIENT per-handle state — alice is still bound to this qIPC
/    handle from check 10's rebind), not a hardcoded (::): current[] correctly falls back to whatever
/    is bound to the handle once the request-scoped principal clears, per its own documented
/    contract ("prefers [reqPrincipal] over the one bound to this qIPC handle") — asserting a
/    hardcoded (::) here would make this check order-dependent on there being no ambient bind.
ph:{[x] current[]};                                              / echoes whichever principal is in effect
hdrs:(enlist `$"X-KX-Principal")!enlist "{\"sub\":\"dave\"}";    / deliberately mixed-case header key
before:current[];                                                / the ambient state this handle already has
c13a:`dave~(serveHttp[ph;("";hdrs)])`sub;                        / matched despite the header's casing
c13b:before~current[];                                           / cleared after a normal return -> back to ambient
c13c:before~serveHttp[ph;("";(`$())!())];                        / no header -> prior handler sees the SAME ambient
phErr:{[x] '"boom"};
e13:@[{serveHttp[phErr;("";hdrs)]};(::);{x}];
c13d:("boom"~e13) and (before~current[]);                        / raised, but STILL cleared back to ambient
r13:c13a and c13b and c13c and c13d;

/ 14. activateHttp[] composes with a PRIOR .z.ph rather than clobbering it (mirrors activate[]'s
/    documented composition contract for .z.pw/.z.po/.z.pc, now proven for the HTTP pair too).
markerHit:0b;
.z.ph:{[x] markerHit::1b; `ok};
activateHttp[];
.z.ph[("";(`$())!())];
r14:markerHit;

flags:(r1;r2;r3;r4;r5;r6;r7;r8;r9;r10;r11;r12;r13;r14);
-1 "kx.auth assert-gate flags r1..r14 = ",`char$48+flags;
$[all flags; [-1 "ALL PASS"; exit 0]; [-1 "FAIL"; exit 1]];
