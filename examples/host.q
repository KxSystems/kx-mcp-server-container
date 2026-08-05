// host.q — minimal KDB-X host for local smoke testing the composition container.
//
// Initializes the SQL module's compatibility interface (.s), loads the AI libraries (.ai), seeds
// canonical annotated tables, and listens on :5010 (the kdbx bundle's default). When the optional
// kx.aimeta module is installed, the annotations compile and become available to MCP discovery;
// otherwise the same host remains a valid native-introspection example.
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
/ @kind data
/ @name instruments
/ @desc Reference data for the instruments represented in the trade tape.
/ @reference instrument
/ @col sym {symbol} Instrument symbol. @attr:u
/ @col name {symbol} Human-readable instrument name. @label
/ @col sector {symbol} Business sector. @cardinality:low
/ @tag reference
instruments:([]
  sym   :`u#`AAPL`MSFT`GOOG`AMZN`NVDA;
  name  :`Apple`Microsoft`Alphabet`Amazon`NVIDIA;
  sector:`Technology`Technology`Communication`Consumer`Technology
 );

/ @kind data
/ @name trades
/ @desc Executed equity trades used by the MCP query and metadata examples.
/ @col time {timestamp} Execution timestamp in UTC.
/ @col sym {symbol} Instrument symbol. @semanticType:instrument @foreignRef:instruments.sym @attr:g
/ @col side {symbol} Buy or sell side. @cardinality:low
/ @col price {float} Execution price.
/ @col size {long} Executed quantity in shares.
/ @sampleRow 2024.01.02D09:30:00.000000000,`AAPL,`B,187.45,100
/ @tag core
trades:([]
  time : 2024.01.02D09:30:00.000000000 + 1000000000 * til 10;
  sym  : 10#`AAPL`MSFT`GOOG`AMZN`NVDA;
  side : 10#`B`S;
  price: 187.45 411.22 142.18 155.03 720.91 188.10 410.85 142.55 154.60 722.34;
  size : 100 250 75 500 40 120 300 60 450 35
 );

// Apply the runtime attribute declared above so native and annotated metadata agree.
@[`trades;`sym;`g#];

/ @kind function
/ @name .analytics.vwap
/ @desc Volume-weighted average execution price for one or more instruments.
/ @public
/ @param syms {symbol[]} Instrument symbols to include.
/ @returns {table} Symbol-keyed table with one vwap column.
/ @example .analytics.vwap[`AAPL`MSFT]
/ @uses trades
/ @tag stable
.analytics.vwap:{[syms] select vwap:size wavg price by sym from trades where sym in syms};

// --- listen -----------------------------------------------------------------
// Default to :5010, but honor a -p passed on the command line (q applies -p before this script
// runs). NB on macOS, port 5000 is taken by Control Center / AirPlay Receiver — hence :5010.
if[0=system"p"; system"p 5010"];

// aimeta is optional. Its init compiles the annotation blocks above and publishes the canonical
// document over qIPC; failure to load the module leaves the MCP backend's native fallback intact.
.demo.aimetaLoaded:1b;
aimeta:@[use;`kx.aimeta;{[err]
  .demo.aimetaLoaded:0b;
  -1 "  aimeta: not loaded (native metadata fallback): ",err;
  ::}];
if[.demo.aimetaLoaded; aimeta[`init][]];

// --- report -----------------------------------------------------------------
-1 "KDB-X host ready on :",string system"p";
-1 "  .s items: ",string count key `.s;
-1 "  .ai items: ",string count key `.ai;
-1 "  tables: ",", " sv string tables[];
-1 "  metadata: ",$[.demo.aimetaLoaded;"aimeta annotations";"native introspection"];
