/ The host that produced meta-v0.2.0.json and meta-v0.3.0.json: real aimeta documents, captured as
/ `.j.j .aimeta.data[]`, which is exactly what the backend fetches over qIPC.
/ Captured 2026-09-29 by loading this file with aimeta v0.2.0 (schemaVersion 2) and then v0.3.0
/ (schemaVersion 3) as kx/aimeta on QPATH, with the kx.log / kx.printf / kx.ax / kx.fusion runtime.
/ The `@authorize` line is v0.3.0-only, so it was removed for the v0.2.0 capture.
/ Re-capture from a directory holding only this file (aimeta compiles every .q file under it):
/   QPATH=<dir holding kx/aimeta at the tag> q capture-host.q -q meta-vX.Y.Z.json
aimeta:use`kx.aimeta;

/ @kind data
/ @name instruments
/ @desc Reference data for the instruments traded.
/ @col sym {symbol} Instrument symbol.
/ @col sector {symbol} Business sector.
instruments:([] sym:`AAA`BBB; sector:`tech`energy);

/ @kind data
/ @name trades
/ @desc Executed trades, one row per fill.
/ @col sym {symbol} Instrument symbol. @semanticType:instrument @foreignRef:instruments.sym
/ @col price {float} Execution price.
/ @col size {long} Executed quantity.
trades:([] sym:`AAA`BBB`AAA; price:10.5 20.25 10.75; size:100 200 300);

/ @kind function
/ @name .demo.vwap
/ @desc Volume-weighted average price per symbol.
/ @public
/ @param syms {symbol[]} Instrument symbols to include.
/ @returns {table} Symbol-keyed table with one vwap column.
/ @uses trades
/ @authorize read data.trades
.demo.vwap:{[syms] select vwap:size wavg price by sym from trades where sym in syms};

aimeta[`init][];
(hsym`$first .z.x) 0: enlist .j.j .aimeta.data[];
exit 0
