/ Regression for the kx.auth identity-assertion gate: bind[] asks the SAME authorization policy WHO
/ may assert — an S/A/R decision keyed on the caller's login (.z.u, passed as `sub`) with action
/ `assert on resource `identity, default-deny (policy is deny-all until setPolicy). "Who may assert"
/ is a host grant in the one policy, not a module built-in. Exits 0 on ALL PASS / 1 on any failure so
/ the pytest wrapper can assert on the return code. Run directly: `q <this file>` (needs a license).
/ .
/ This covers the module LOGIC; the live .z.u-over-IPC property (a real second connection under a
/ different login is refused at bind) is exercised by a live demo / the kdb-x realidp lane. In-process
/ .z.u is fixed, so we grant `assert to whatever .z.u is and prove caller-keying by also granting it
/ to a DIFFERENT login and confirming bind is then refused.
\l modules/kx/auth/init.q
u:.z.u;   / the in-process caller login — what bind passes to the policy as the subject's `sub`

/ 1. default-deny: policy unset (deny-all) -> bind refuses (a mere connection is not enough).
e1:@[{bind[()!()];`ok};(::);{x}];
r1:$[10h=type e1; "denied"~6#e1; 0b];

/ 2. a policy granting `assert on `identity to THIS caller -> bind succeeds; current[] reflects the
/    ASSERTED principal (whose `sub differs from the caller — proving caller != asserted subject).
setPolicy[{[p;a;r] (a~`assert) and (r~`identity) and p[`sub]~u}];
bind[`sub`groups!(`enduser;`viewer`trader)];
r2:`enduser~(current[])`sub;

/ 3. grant `assert only to a DIFFERENT login -> bind refuses (proves it keys on the caller .z.u).
setPolicy[{[p;a;r] (a~`assert) and (r~`identity) and p[`sub]~`otheruser}];
e3:@[{bind[()!()];`ok};(::);{x}];
r3:$[10h=type e3; "denied"~6#e3; 0b];

/ 4. the data S/A/R path is unaffected: assert allowed, `read`trades granted to `trader, others denied.
setPolicy[{[p;a;r] $[a~`assert; 1b; (a~`read) and (r~`trades) and `trader in p`groups]}];
bind[`sub`groups!(`alice;enlist`trader)];                    / enlist: mirror PyKX's 1-group vector shape
r4:`trader in (authorize[`read;`trades])`groups;             / granted -> returns the principal
e5:@[{authorize[`read;`secrets];`ok};(::);{x}];
r5:$[10h=type e5; "denied"~6#e5; 0b];                        / ungranted -> 'denied (default-deny governs)

flags:(r1;r2;r3;r4;r5);
-1 "kx.auth assert-gate flags r1..r5 = ",`char$48+flags;
$[all flags; [-1 "ALL PASS"; exit 0]; [-1 "FAIL"; exit 1]];
