/ Regression for kx.auth's per-handle principal REPLACEMENT semantics (KXI-72739).
/ .
/ bind[] must replace a handle's principal WHOLESALE. It previously stored principals as the values of
/ a dict, and a dict whose values are conforming dicts IS a keyed table to q — so the store-join became
/ a COLUMN-WISE upsert. Two defects fell out of that, both covered below:
/   * a re-bind with a NARROWER key set kept the previous principal's extra fields, so a stale `tenant`
/     (or `groups`, `exp`, `act` …) outlived a token refresh and a policy could authorise on it. The
/     container caches connections on (sub;iss) and re-binds on every call, so a refreshed token for
/     the same user reuses the same handle — this was reachable on the ordinary path, not a corner.
/   * binding a narrower principal on a SECOND handle 'mismatch'ed outright (schema conformance),
/     i.e. one connected user could make another user's bind fail.
/ A third, independent defect is covered too: promote[] index-assigned symbol VECTORS into the incoming
/ dict, which 'type'd whenever that dict had a uniform TYPED value list (an all-atoms principal such as
/ `sub`iss!(`a;`b)) — so bind[] rejected a perfectly valid principal.
/ .
/ Exits 0 on ALL PASS / 1 on any failure so the pytest wrapper can assert on the return code.
/ Run directly: `q <this file>` (needs a license). Handles are faked by calling bind[] in-process
/ (.z.w is 0i) for the same-handle cases, and by driving the store directly for the two-handle case.
/ .
/ Every check is a self-contained trapped lambda: the pre-fix module SIGNALS on several of these, and a
/ signal must fail only its own check rather than aborting the file and hiding the rest.
\l modules/kx/auth/init.q
setPolicy[{[p;a;r] 1b}];   / assert allowed throughout — this file is about the store, not the gate
ok:{[f] $[`err~r:@[f;(::);{`err}]; 0b; r]};   / run a check, mapping any signal to a plain failure
reset:{[] bound::(`int$())!(); };            / empty the whole store between checks (any handle)

/ ---- 1. the ticket repro: a re-bind with a NARROWER key set drops the stale field ----------------
r1:ok {reset[];
  bind[`sub`groups`tenant!(`alice;`x`y;`acme)];
  a:`acme~(current[])`tenant;                       / precondition: tenant really was bound
  bind[`sub`groups!(`bob;`z`w)];
  a and not `tenant in key current[] };             / and is gone after a narrower re-bind

/ ---- 2. a re-bind is an EXACT replacement, not a merge -------------------------------------------
r2:ok {reset[];
  bind[`sub`groups`exp`act!(`alice;`x`y;1893456000;(enlist `sub)!enlist "svc")];
  bind[`sub`groups!(`bob;`z`w)];
  (`sub`groups`tenant`exp`act inter key current[])~`sub`groups };   / only sub+groups survive

/ ---- 3. a stale principal cannot outlive a re-bind into a POLICY decision ------------------------
/ The security consequence of 1: a tenant-scoped policy must not authorise bob on alice's tenant.
r3:ok {reset[];
  setPolicy[{[p;a;r] $[a~`assert; 1b; (r~`trades) and `acme~p`tenant]}];
  bind[`sub`tenant!(`alice;`acme)];
  a:`acme~(authorize[`read;`trades])`tenant;        / alice IS in acme -> allowed
  bind[(enlist `sub)!enlist `bob];                  / refreshed token, no tenant claim at all
  e:@[{authorize[`read;`trades];`ok};(::);{x}];
  setPolicy[{[p;a;r] 1b}];
  a and $[10h=type e; "denied"~6#e; 0b] };          / bob must NOT inherit acme -> denied

/ ---- 4. the structural invariant that makes a column-wise upsert impossible ----------------------
/ `bound`'s value list must stay a GENERAL list (0h). The moment its values are bare conforming dicts
/ it is a keyed table and the store-join acquires column semantics — the root cause of 1-3. Asserted
/ against the module's own store after a real bind, so it pins the representation, not a local copy.
r4:ok {reset[]; bind[`sub`groups`tenant!(`alice;`x`y;`acme)]; 0h=type value bound };

/ ---- 5. a narrower bind is unaffected by ANOTHER handle holding a wider principal ----------------
/ In-process .z.w is fixed, so the second handle is seeded by cloning the module's OWN stored value
/ (no test-local copy of the store shape) onto a spare handle. Pre-fix this either 'mismatch'ed or
/ merged; the live two-handle property over real IPC is covered by the kdb-x realidp lane.
r5:ok {reset[];
  bind[`sub`groups`tenant!(`alice;`x`y;`acme)];     / handle 0 -> wide
  bound::bound,(enlist 9i)!enlist bound .z.w;       / spare handle 9i also holds the wide principal
  bind[`sub`groups!(`bob;`z`w)];                    / narrower re-bind: must neither signal nor merge
  (`bob~(current[])`sub) and not `tenant in key current[] };

/ ---- 6. promote[]/bind[] accept a UNIFORM-typed principal (previously 'type) ---------------------
/ An all-atoms dict has a typed value list; promoting must widen it rather than fail.
r6:ok {reset[]; bind[(enlist `sub)!enlist `bob]; `bob~(current[])`sub };
r6:r6 and ok {`a~(promote `sub`iss!(`a;`b))`sub };
r6:r6 and ok {(enlist `kxmcp)~(promote `sub`aud!(`carol;`kxmcp))`aud };
r6:r6 and ok {(enlist `openid)~(promote `sub`scopes!(`carol;`openid))`scopes };

/ ---- 7. promotion itself is unchanged: claims-sourced groups + exp canon still hold --------------
r7:ok {reset[];
  setClaims[(enlist `groups)!enlist "realm_access.roles"];
  cl:(`realm_access`sub)!(((enlist `roles)!enlist ("trader";"viewer"));"alice-uuid");
  bind[`sub`client`scopes`claims`exp!(`$"alice-uuid";`kxmcp;("openid";"profile");cl;1893456000)];
  p:current[];
  (`trader`viewer~p`groups) and (-12h=type p`exp) and valid[] and `openid`profile~p`scopes };

/ ---- 8. clear[] still fully resets a handle (the disconnect path) --------------------------------
r8:ok {reset[]; bind[`sub`tenant!(`alice;`acme)]; clear .z.w; (::)~current[] };

flags:(r1;r2;r3;r4;r5;r6;r7;r8);
-1 "kx.auth rebind flags r1..r8 = ",`char$48+flags;
$[all flags; [-1 "ALL PASS"; exit 0]; [-1 "FAIL"; exit 1]];
