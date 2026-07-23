// host.q — minimal KDB-X host for local smoke testing the composition container.
//
// Initializes the SQL module's compatibility interface (.s), loads the AI libraries (.ai), seeds
// one canonical table, and listens on :5010 (the kdbx bundle's default).
//
// Run:  q examples/host.q
//   (a standard kdb-x install puts `q` on PATH and resolves its own home + license)

// --- initialize the kdb-x modules -------------------------------------------
// The SQL module has a special compatibility integration: its installer places s.k_ on the q
// runtime path and preserves `.s.init[]` as the stable initialization interface. The optional AI
// libraries use the normal module framework and return their namespace from `use`, so assign that
// result to `.ai`.
.ai:use`kx.ai;     // AI libraries   -> enables similarity/hybrid search tools (.ai ~ 10 items)
.s.init[];         // populate .s (~238 items) -> the kdbx_run_sql_query tool + .s.e

// --- seed canonical data ----------------------------------------------------
// Realistic trade rows so the SQL tool / describe-tables resource have something to return.
trades:([]
  time : 2024.01.02D09:30:00.000000000 + 1000000000 * til 10;
  sym  : 10#`AAPL`MSFT`GOOG`AMZN`NVDA;
  side : 10#`B`S;
  price: 187.45 411.22 142.18 155.03 720.91 188.10 410.85 142.55 154.60 722.34;
  size : 100 250 75 500 40 120 300 60 450 35
 );

// --- listen -----------------------------------------------------------------
// Default to :5010, but honor a -p passed on the command line (q applies -p before this script
// runs). NB on macOS, port 5000 is taken by Control Center / AirPlay Receiver — hence :5010.
if[0=system"p"; system"p 5010"];

// --- report -----------------------------------------------------------------
-1 "KDB-X host ready on :",string system"p";
-1 "  .s items: ",string count key `.s;
-1 "  .ai items: ",string count key `.ai;
-1 "  tables: ",", " sv string tables[];
