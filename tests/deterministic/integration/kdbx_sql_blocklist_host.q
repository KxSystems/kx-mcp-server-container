// kdbx_sql_blocklist_host.q — minimal real-q backbone for the SQL write-keyword-blocklist bypass
// regression. No identity assertion, no kx.auth — plain q with `.s.init[]` and a real table, so a
// SELECT-prefixed, semicolon-chained destructive statement can be proven to (not) reach `.s.e`
// unchallenged.
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

-1 "";
-1 "kdbx_sql_blocklist_host ready: plain q, no identity assertion, `trades table live";
