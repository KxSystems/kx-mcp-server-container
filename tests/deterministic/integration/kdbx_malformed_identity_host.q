// kdbx_malformed_identity_host.q — real-q backbone for the non-string `sub`-claim regression.
//
// A single, unconditional policy comparing the principal's `sub` to the service-account login
// symbol does double duty: the bind-gate check passes a caller dict with sub=.z.u (a symbol) ->
// matches -> bind allowed silently. `.s.e` is wrapped (the "ferry host" pattern — see
// kdbx_ferry_host.q) to call `.kx.auth.authorize` inline on every query, using the BOUND
// principal (sub=12345, a q long, ferried from a malformed token) -> the exact same comparison
// -> symbol = long -> a raw q 'type error, not the module's own `denied: convention. This must go
// through the WRAPPED-.s.e path, not the KDBX_DB_DATA_GATE/consult_data_gate path — decide()'s own
// adapter-exception fail-closed wrapper already converts any raised exception into a clean deny,
// which would accidentally mask this exact bug.
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

// --- identity assertion (the kx.auth KDB-X module) ------------------------------------------------
.kx.auth:use`kx.auth;

.gate.svcUser:`kxmcp;
.gate.svcPassword:"kdbx-malformed-identity-test-svc-pw";

.kx.auth.configure[(.gate.svcUser; .gate.svcPassword)];
.kx.auth.activate[];

// --- the deliberately unconditional, type-clash-triggering policy ---------------------------------
.gate.allowed:{[p;a;r] .gate.svcUser=p[`sub]};
.kx.auth.setPolicy[.gate.allowed];

// Gate every SQL query through the seam (the ferry-host pattern) so the policy's type clash
// actually fires on a real query, not just at bind time.
.s.realE:.s.e;
.s.e:{[x] .kx.auth.authorize[`read;`data.trades]; .s.realE x};

-1 "";
-1 "kdbx_malformed_identity_host ready: identity assertion ON, .s.e wrapped, policy compares sub to a symbol";
