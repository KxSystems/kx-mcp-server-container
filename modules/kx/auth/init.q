/ kx.auth — q-side identity assertion for the KX MCP container. A KDB-X module.
/ .
/ The container validates the inbound JWT, projects the claims into a q dict, connects as a trusted
/ service account, and calls .kx.auth.bind[principal] on the handle. q does NO token parsing or
/ crypto — it trusts the assertion because it arrived on a service-account connection (ideally TLS).
/ Permission-check functions then consult current[] / require[] to gate access off the caller.
/ .
/ Two identities: the service account authenticates the *connection* (via .z.pw, wired by activate[]);
/ the *asserted principal* never logs in — it is bound post-connect. bind[] does NOT trust the mere
/ connection: it asks the SAME authorization policy WHO may assert — an S/A/R grant keyed on the
/ caller's login .z.u, action `assert on `identity (default-deny). So "who may assert" is a host grant
/ in the one policy (see the demo), not a module built-in.
/ .
/ Load (the consumer MUST assign it to the global `.kx.auth` so the container's qIPC call
/ `.kx.auth.bind` resolves — see the design-doc caveat):
/     .kx.auth:use`kx.auth;
/     .kx.auth.configure[(`kxmcp;"service-account-password")];   / set the service account
/     .kx.auth.activate[];                                        / wire .z.pw/.z.po/.z.pc (qIPC)
/     .kx.auth.activateHttp[];                                    / OPTIONAL: wire .z.ph/.z.pp (thin HTTP)
/ .
/ A host with a data-level authz policy installs it once: .kx.auth.setPolicy[myGrantFn]; thereafter
/ permission-check functions call .kx.auth.authorize[action;resource] (default-deny until set).
/ .
/ Everything here lives in the module's private namespace; only the `export` dict at the foot is
/ visible. Module-global state is mutated with `::`.
/ NB: a solitary "/" line would open a block comment that silently voids the rest of the file (see
/ the q skill) — every blank comment line below carries a trailing "." on purpose.

/ ============================ private state ============================
/ Per-handle principal store: connection handle (.z.w) -> a ONE-ROW TABLE wrapping the projected
/ principal dict. One entry per open qIPC handle; cleared on open/close once activate[] has wired the
/ .z handlers. Read an entry back with `first` (see current[]).
/ .
/ That wrapping enlist is LOAD-BEARING. A dict whose values are conforming *dicts* IS a keyed table to
/ q, so storing principals directly makes `bound,(enlist w)!enlist principal` a COLUMN-WISE upsert
/ rather than a replacement. Two ways that bites: rebinding a NARROWER principal on a live handle
/ keeps the previous one's extra fields (a stale `tenant` outliving a token refresh, so a policy
/ authorises on stale identity), and binding a narrower principal on a SECOND handle 'mismatch'es
/ outright. Storing one-row TABLES keeps the value list general, so each handle is replaced WHOLESALE.
bound:(`int$())!();

/ Request-scoped principal for the HTTP path (.z.ph/.z.pp). Unlike qIPC — where one connection = one
/ principal for the life of the handle — an HTTP socket (.z.w) is reused across requests (keep-alive),
/ so identity is bound per-REQUEST, not per-handle: serveHttp sets this for the in-flight request and
/ clears it after. current[] prefers it over the per-handle `bound` table. (::) = no request principal.
reqPrincipal:(::);

/ The header a trusted gateway carries the principal JSON in (thin model: the gateway has already
/ validated the bearer; q trusts the header as it trusts the service-account qIPC connection).
/ Matched case-insensitively. See activateHttp / the design doc's HTTP section.
principalHeader:`$"x-kx-principal";

/ Claim sources for the promoted fields, set by setClaims (a host concern, so q is self-contained for
/ the HTTP path). `groups`/`tenant` -> a dotted claim path (e.g. "realm_access.roles"); an empty
/ groups path falls back to the default IdP search order below. This is the ONE place IdP-variance
/ lives — promote[] reads it; policies never touch raw claims.
claimPaths:`groups`tenant!("";"tenant");
groupSearch:("groups";"realm_access.roles";"roles");   / default order when claimPaths`groups is ""

/ Tier 0 service-account login: the (user;password) the trusted container connects with. Empty by
/ default — an unconfigured svc makes .z.pw defer to any prior handler (dev/no-op). Tier 1
/ (platform-delegated login) is future work; see the design doc.
svc:(`;"");

/ The data-level authorization policy (the S/A/R decision hook): (principal;action;resource) -> 1b.
/ Default-DENY — an unconfigured policy refuses everything, so a host that calls authorize[] without
/ first setPolicy[]-ing gets fail-closed behaviour. The demo/host installs a concrete grant policy.
policy:{[principal;action;resource] 0b};

/ Prior .z handlers, captured by activate[] / activateHttp[] so we compose rather than clobber.
priorPw:{[u;p] 1b};
priorPo:{[w] };
priorPc:{[w] };
priorPh:{[x] };
priorPp:{[x] };

/ ============================ canonicalisation / promotion ============================
/ promote[] is the SINGLE authority that turns a principal — however it arrived (qIPC via PyKX, or
/ HTTP via .j.k) — into the canonical shape policies read: promoted fields symbolised, `groups`/
/ `tenant` extracted from the configured claim path when not already top-level, `exp` a timestamp,
/ raw `claims` left untouched (char vectors — never symbolised, so high-cardinality values like `jti`
/ never intern). Both bind (qIPC) and fromJson (HTTP) route through it, so the two transports converge
/ on one promotion implementation. Idempotent: a pre-promoted principal passes through unchanged.

/ Walk a dotted claim path over a (possibly nested) dict; (::) if any segment is missing. The inner
/ check is NESTED $ (not `and`) so `key r` is only evaluated when r is a dict — `and` is eager and
/ `key (::)` (a missed segment) would 'type.
dig:{[d;path] {[r;k] $[99h=type r; $[k in key r; r k; (::)]; (::)]}/[d; `$"." vs path]};

/ Coerce a value to a symbol atom (char vector -> symbol; already-symbol left as-is).
asSym:{[x] $[10h=type x; `$x; x]};
/ Coerce to a symbol VECTOR: "x"->,`x ; ("a";"b")->`a`b ; `x->,`x ; `a`b left as-is.
asSyms:{[x] $[10h=type x; enlist `$x; (0h=type x) and all 10h=type each x; `$x; -11h=type x; enlist x; x]};

/ Extract groups from the claims: the configured single path, else the default search order; first hit.
extractGroups:{[claims]
  paths:$[count claimPaths`groups; enlist claimPaths`groups; groupSearch];
  v:dig[claims] each paths;
  good:v where not (::)~/:v;
  $[count good; asSyms first good; `$()] };

/ Canonicalise `exp` (unix seconds — a long over qIPC, a float over HTTP) to a q timestamp. Guarded on
/ -7 -9h so a re-promote (already a timestamp, -12h) is not double-converted.
canon:{[p]
  if[(`exp in key p) and (type p`exp) in -7 -9h;
    p[`exp]:1970.01.01D0 + 1000000000 * `long$p`exp];
  p };

promote:{[p]
  c:$[`claims in key p; p`claims; ()!()];
  / groups: prefer a top-level value, else extract from claims (the promotion proper). Done FIRST, and
  / as a dict JOIN rather than an index-assign, which is load-bearing: an all-atoms principal (e.g.
  / `sub`iss!(`a;`b)) has a uniform TYPED value list, and index-assigning a symbol VECTOR into one
  / 'type's. Joining a vector-valued dict widens the value list to a general list, so every assignment
  / below is then safe. `groups` is always set, so leading with it costs nothing.
  p:p,(enlist `groups)!enlist $[`groups in key p; asSyms p`groups; extractGroups c];
  / scalar identity fields -> symbols (idempotent — PyKX already symbolised them over qIPC)
  sf:`sub`client`iss inter key p;
  p[sf]:asSym each p sf;
  if[`aud in key p; p[`aud]:asSyms p`aud];
  if[`scopes in key p; p[`scopes]:asSyms p`scopes];
  / tenant: prefer top-level, else the configured claim
  t:$[`tenant in key p; p`tenant; (count claimPaths`tenant) and 99h=type c; dig[c; claimPaths`tenant]; (::)];
  if[not (::)~t; p[`tenant]:asSym t];
  / sub fallback: derive from the `sub` claim, else the client id (fastmcp leaves subject unset)
  if[(not `sub in key p) or (`~p`sub); p[`sub]:$[`sub in key c; asSym c`sub; p`client]];
  canon p };

/ ============================ core verbs ============================
/ Bind the asserted principal to the current handle. Called by the trusted container right after it
/ connects and before the first query. Does NOT trust the mere connection: it first asks the SAME
/ authorization policy WHO may assert — an S/A/R decision with the CALLER's login (.z.u) as subject
/ and (`assert;`identity) as action/resource (default-deny, since `policy` is deny-all until set).
/ This runs pre-bind, so it consults `policy` directly — not authorize[]/require[], which need an
/ already-bound principal. On allow, promotes (canonicalises) and stores. Idempotent per handle, and a
/ re-bind on a live handle REPLACES the principal wholesale — no field of the previous one survives.
bind:{[principal]
  caller:(enlist `sub)!enlist .z.u;
  if[not policy[caller; `assert; `identity];
    '"denied: caller ",string[.z.u]," not permitted to assert identity (grant `assert on `identity via setPolicy)"];
  / the inner enlist wraps the principal as a one-row table — see the `bound` comment for why that is
  / what makes this a wholesale replacement instead of a column-wise upsert.
  bound::bound,(enlist .z.w)!enlist enlist promote principal; };

/ The principal in effect: the per-request HTTP principal if one is set, else the one bound to this
/ qIPC handle, else the unbound sentinel (::). One accessor serves both transports. `first` unwraps the
/ one-row table `bound` stores each principal as.
current:{[] $[not (::)~reqPrincipal; reqPrincipal; .z.w in key bound; first bound .z.w; (::)]};

/ 1b iff a present, unexpired principal is in effect (qIPC handle or HTTP request). `exp` is a q
/ timestamp (canon), so compare it directly to now. No `exp` -> treated as non-expiring.
valid:{[]
  p:current[];
  if[(::)~p; :0b];
  if[not `exp in key p; :1b];
  p[`exp] > .z.p };

/ Default-deny gate for permission-check functions. Signals 'denied when no valid principal is
/ bound, so a query on an unbound/expired handle is refused rather than running as the service
/ account. Returns the principal dict when valid.
require:{[]
  if[not valid[]; '"denied: no valid principal bound to this handle"];
  current[] };

/ ============================ authorization seam (S/A/R) ============================
/ The data-level Subject / Action / Resource gate. Subject = the bound principal (require[] enforces a
/ valid one first, default-deny); Action / Resource are caller-supplied symbols (e.g. `read on `trades).
/ Defers the allow/deny decision to the installed policy (default-deny). Signals 'denied on refusal;
/ returns the principal on allow so a caller can `p:.kx.auth.authorize[`read;`trades]` and use it.
authorize:{[action;resource]
  principal:require[];
  if[not policy[principal;action;resource];
    '"denied: ",string[principal`sub]," not permitted ",string[action]," on ",string resource];
  principal };

/ Install the authorization policy — a (principal;action;resource) -> 1b decision function. This is
/ the seam a host loads its entitlements into; until set, authorize[] is fail-closed (default-deny).
setPolicy:{[fn]
  if[not (type fn) within 100 112h; '"kx.auth.setPolicy: expects a function (principal;action;resource) -> boolean"];
  policy::fn; fn };

/ The scope-down companion to authorize[]: return the subset of `resources` the bound principal may
/ perform `action` on, in ONE round-trip. Same default-deny posture — require[] signals 'denied when
/ no valid principal is bound, and an unset policy yields the empty subset (never a silent allow).
/ Accepts an atom or a vector; always returns a vector. This is what the container's kdb-x
/ entitlements adapter (the data gate) consults to allow / deny / scope-down a request over several
/ resources without N exception-driven authorize[] calls; a filtered subset is how the authorization
/ seam sources its allow-with-obligations answer.
entitled:{[action;resources]
  p:require[];
  rs:(),resources;
  $[0=count rs; rs; rs where policy[p;action;] each rs] };

/ ============================ login + handle lifecycle ============================
/ Drop a handle's binding (on connect — guards id reuse — and on disconnect).
clear:{[w] bound::bound _ w; };

/ Service-account check; defers to the prior .z.pw when svc is unconfigured.
pwCheck:{[u;p] $[(`~svc 0)and 0=count svc 1; priorPw[u;p]; (u~svc 0)and p~svc 1]};

/ ============================ HTTP path (thin) ============================
/ Parse the gateway's principal JSON and run it through the shared promote[] — the SAME canonicaliser
/ qIPC's bind uses, so both transports yield an identical principal shape (promoted fields symbolised,
/ groups extracted from claims, raw claims left as char vectors). .j.k already yields char vectors, so
/ no high-cardinality value interns here either.
fromJson:{[json] promote .j.k json};

/ HTTP request handler wrapper (composes with the prior .z.ph/.z.pp). Thin model: a trusted gateway
/ has already validated the bearer and carries the projected principal as JSON in principalHeader; we
/ bind it for THIS request only (reqPrincipal), run the prior handler, then clear — even on error.
/ x is kdb+'s (requestText; headerDict) pair; headers have symbol keys, matched case-insensitively.
/ NB `prior` is a reserved q keyword (named each-prior adverb) — using it as a param name 'nyi's, so
/ the prior handler is `ph` here.
serveHttp:{[ph;x]
  hdrs:$[1<count x; x 1; (`$())!()];
  k:key hdrs;
  i:(lower k)?principalHeader;
  reqPrincipal::$[i<count k; fromJson (hdrs k i); (::)];   / parens: (hdrs k i) is the value, THEN fromJson
  r:@[ph; x; {[e] reqPrincipal::(::); 'e}];
  reqPrincipal::(::);
  r };

/ Set the service account. Expects a 2-item (user;password) — (`symbol; "string").
configure:{[s]
  if[2<>count s; '"kx.auth.configure: expects (`user;\"password\")"];
  svc::s; svc };

/ Configure the claim sources for the promoted fields. Accepts a dict keyed on `groups`/`tenant` with
/ dotted-path string values, e.g. `groups`tenant!("realm_access.roles";"tenant"). Partial updates
/ merge; an empty/absent `groups` path keeps the default search order. A host calls this once so q
/ (not the container) owns where promotion reads from — which is what makes the HTTP path self-contained.
setClaims:{[d]
  if[not 99h=type d; '"kx.auth.setClaims: expects a dict, e.g. `groups`tenant!(\"realm_access.roles\";\"tenant\")"];
  claimPaths::claimPaths,d; claimPaths };

/ Wire the process .z handlers (composing with any prior ones). Side-effect-free until called, so
/ a bare `use` never silently changes process login. The installed lambdas retain this module's
/ namespace, so they reach the private `bound` / `svc` state directly.
activate:{[]
  priorPw::@[value;`.z.pw;{[e] {[u;p] 1b}}];
  priorPo::@[value;`.z.po;{[e] {[w] }}];
  priorPc::@[value;`.z.pc;{[e] {[w] }}];
  .z.pw:{[u;p] pwCheck[u;p]};
  .z.po:{[w] clear w; priorPo w};
  .z.pc:{[w] clear w; priorPc w};
  };

/ Wire the HTTP request handlers for the thin-HTTP posture (kdb+ served behind a trusted gateway).
/ Separate from activate[] — opt-in, leaves the qIPC path untouched — and likewise composes with any
/ prior .z.ph/.z.pp. Reference wiring for the design doc's HTTP section; the projection + per-request
/ binding logic is exercised by tests, the full gateway round-trip is the deployment's concern.
activateHttp:{[]
  priorPh::@[value;`.z.ph;{[e] {[x] }}];
  priorPp::@[value;`.z.pp;{[e] {[x] }}];
  .z.ph:{[x] serveHttp[priorPh;x]};
  .z.pp:{[x] serveHttp[priorPp;x]};
  };

/ ============================ public surface ============================
export:`bind`current`valid`require`authorize`entitled`setPolicy`configure`setClaims`activate`activateHttp!(bind;current;valid;require;authorize;entitled;setPolicy;configure;setClaims;activate;activateHttp);
