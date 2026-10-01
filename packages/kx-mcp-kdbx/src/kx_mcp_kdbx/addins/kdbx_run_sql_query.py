import logging
import re
import pykx as kx
import json
from typing import Dict, Any
from fastmcp import Context
from fastmcp.tools import tool
from kx_mcp_kdbx.utils import kdbx as kdbx_utils
from kx_mcp_kdbx.utils.kdbx import get_kdb_connection, config_from_ctx
from kx_mcp_kdbx.utils.denial import denial_response, denial_from_decision
from kx_mcp_kdbx.utils.authz_kx_entitlements import consult_data_gate, derive_tables
from kx_mcp_kdbx.utils.observe import q, record_sql_result
from kx_mcp_kdbx.utils.non_finite import NON_FINITE_ERROR_TYPE, has_non_finite
from kx_mcp_core import tool_result
from kx_mcp_core.auth import authorize

logger = logging.getLogger(__name__)
MAX_ROWS_RETURNED = 1000

# --- the coarse write guard --------------------------------------------------------------------
#
# Not authorization — the S/A/R seam is that. This is the no-regression floor inherited from the
# standalone server, kept as defence in depth in front of it.
#
# It used to be dead code. The check read `if keyword in query_upper and not
# query_upper.startswith('SELECT')`, so the `startswith` test — evaluated against the WHOLE query,
# once per keyword — short-circuited every keyword the moment a query began with SELECT. Since q's
# SQL interface accepts `;`-chained statements, `SELECT * FROM trades; DROP TABLE trades` sailed
# straight through and actually dropped the table (reproduced live against a real q process).
#
# Two changes, and they are load-bearing together. Chained statements are now rejected outright,
# which is what actually closes the bypass. And the keyword scan matches on WORD BOUNDARIES over a
# literal-stripped statement, which is what makes removing the `startswith` exemption safe: that
# exemption was the only thing preventing mass false positives, because a bare substring test reads
# `CREATE` inside a perfectly ordinary column named `created_at`.
_DANGEROUS_KEYWORDS = ('INSERT', 'DROP', 'DELETE', 'TRUNCATE', 'ALTER', 'CREATE')

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _strip_literals(query: str) -> str:
    """Blank out single-quoted literals so the guard reads SQL syntax, not user data.

    Keeps `SELECT 'INSERT successful' AS message` legal — a keyword inside a string is data.
    """
    return _STRING_LITERAL.sub("''", query)


def _statements(query: str) -> list:
    """Split on `;` *outside* string literals; drop empty fragments (so a trailing `;` is fine).

    Hand-rolled rather than `query.split(';')` because a semicolon inside a literal
    (`WHERE note = 'a;b'`) is not a statement separator, and splitting on it would reject a
    legitimate query. `''` is SQL's escape for a quote inside a literal.
    """
    parts: list = []
    buf: list = []
    in_literal = False
    i = 0
    while i < len(query):
        char = query[i]
        if in_literal:
            if char == "'":
                if query[i + 1:i + 2] == "'":  # doubled quote — an escaped quote, still inside
                    buf.append("''")
                    i += 2
                    continue
                in_literal = False
            buf.append(char)
        elif char == "'":
            in_literal = True
            buf.append(char)
        elif char == ';':
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(char)
        i += 1
    parts.append(''.join(buf))
    return [stripped for stripped in (part.strip() for part in parts) if stripped]


def check_write_guard(query: str) -> None:
    """Raise ``ValueError`` unless ``query`` is a single statement with no write keyword.

    Separate from ``run_query_impl`` so the guard is directly unit-testable: the e2e proof of the
    bypass needs a real q process and therefore self-skips wherever no `q` binary exists — which
    includes every CI image — so the CI-visible coverage of this guard has to be at this level.
    """
    statements = _statements(query)
    if not statements:
        raise ValueError("Query is empty")
    if len(statements) > 1:
        raise ValueError(
            f"Query must be a single statement (found {len(statements)}); "
            "`;`-chained statements are not accepted"
        )
    statement_upper = _strip_literals(statements[0]).upper()
    for keyword in _DANGEROUS_KEYWORDS:
        if re.search(rf"\b{keyword}\b", statement_upper):
            raise ValueError(f"Query contains dangerous keyword: {keyword}")



# Capability check: the @authorize decorator runs inside the tool's own context, reads the inbound
# principal, and consults the configured authz strategy (KX_MCP_AUTHZ) over an MCP-semantic
# (action;resource) — here `query`/`kdbx.sql`. For kdb-x set KX_MCP_AUTHZ=kdbx_rbac to route this to
# the q `.kx.auth` policy engine over a *capability* grant set, distinct from the q-side data gate
# applied in the body via `.s.e`. Unset = route-only (allow). A capability deny raises
# AuthorizationDenied (a clean tool error) — distinct from the data-layer denial below, which the
# except-clause maps to a structured permission_denied.
@authorize(action="query", resource="kdbx.sql")
def run_query_impl(sqlSelectQuery: str, config=None) -> Dict[str, Any]:
    try:
        check_write_guard(sqlSelectQuery)

        conn = get_kdb_connection(config)

        # Data gate (opt-in via KDBX_DB_DATA_GATE): explicitly consult the q-side entitlement gate
        # for the tables this query references, on the handle get_kdb_connection just bound
        # (in-tool, so require[] sees the principal — the bind-ordering constraint). Deny →
        # structured permission_denied; scope-down (partial entitlement) → the same envelope
        # carrying the entitled subset as guidance, since rewriting the SQL down to it is out of
        # scope — the agent re-scopes and re-issues. Full allow → run unchanged.
        cfg = config if config is not None else kdbx_utils.db_config
        if getattr(cfg, "data_gate", False):
            known_tables = [str(t) for t in q(conn, "tables", 'tables[]').py()]
            referenced = derive_tables(sqlSelectQuery, known_tables)
            if referenced:
                decision = consult_data_gate("read", referenced)
                if not decision.allowed or decision.obligations.get("denied"):
                    logger.info("Query denied at the data layer by the entitlements gate")
                    return denial_from_decision(decision)

        # below query gets kdbx table data back as json for correct conversion of different datatypes
        result = q(conn, "sql", '{r:.s.e x;`rowCount`data!(count r;.j.j y sublist r)}', kx.CharVector(sqlSelectQuery), MAX_ROWS_RETURNED)
        total = int(result['rowCount'])
        record_sql_result(total, MAX_ROWS_RETURNED)
        if 0==total:
            return {"status": "success", "data": [], "message": "No rows returned"}
        # parse json result
        payload = result['data'].py()
        try:
            rows = json.loads(payload.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as parse_error:
            # `.j.j` emits bare `inf` for +/-infinity (see utils/non_finite.py).
            if has_non_finite(payload):
                logger.error("Query result contains non-finite floats; .j.j emitted invalid JSON")
                return {
                    "status": "error",
                    "error_type": NON_FINITE_ERROR_TYPE,
                    "message": (
                        "The query succeeded but its result contains non-finite floating-point "
                        "values (infinity or NaN), which cannot be represented in JSON. Filter or "
                        "cast those values in the query — e.g. exclude rows where the column is "
                        "infinite, or coerce them to null."
                    ),
                    "technical_details": str(parse_error),
                }
            raise
        if total > MAX_ROWS_RETURNED:
            logger.info(f"Table has {total} rows. Query returned truncated data to {MAX_ROWS_RETURNED} rows.")
            return {
                "status": "success",
                "data": rows,
                "message": f"Showing first {MAX_ROWS_RETURNED} of {total} rows",
            }

        logger.info(f"Query returned {total} rows.")
        return {"status": "success", "data": rows}

    except Exception as e:
        logger.error(f"Query failed: {e}")
        denied = denial_response(e)
        if denied is not None:
            logger.info("Query denied at the data layer by identity-assertion permission check")
            return denied
        if ".s.e" in str(e):
            logger.error("It looks like the SQL interface is not loaded. You can load it manually by running .s.init[]:")
            return {
                "status": "error",
                "error_type": "sql_interface_not_loaded",
                "message": "It looks like the SQL interface is not loaded in the KDB-X database. Please initialize it by running `.s.init[]` in your KDB-X session, or contact your system administrator.",
                "technical_details": str(e)
            }
        return {"status": "error", "message": str(e)}


# Standalone @tool decorator (FastMCP 3.x native discovery): it attaches __fastmcp__ metadata
# and returns the function UNCHANGED, so this module-level object is both the discoverable tool
# and a directly-callable coroutine. The container supplies the `kdbx` qualifier via
# mount(namespace="kdbx"), so the composed tool is `kdbx_run_sql_query` (not double-prefixed).
# The @authorize capability check lives on the separate run_query_impl (below), not stacked on
# this wrapper, so there is no decorator-ordering concern.
@tool(annotations={"readOnlyHint": True})
async def run_sql_query(query: str, ctx: Context) -> Dict[str, Any]:
    """
    Execute a SQL query and return structured results only to be used on kdb and not on kdbai.

    This function processes SQL SELECT statements to retrieve data from the underlying
    database. It parses the query, executes it against the data source, and returns
    the results in a structured format suitable for further analysis or display.

    Use the kdbx_sql_query_guidance resource when creating queries


    Supported query types:
        - SELECT statements with column specifications
        - WHERE clauses for filtering
        - ORDER BY for result sorting
        - LIMIT for result pagination
        - Basic aggregation functions (COUNT, SUM, AVG, etc.)

    For query syntax and examples, see: file://guidance/kdbx-sql-queries

    Args:
        query (str): SQL SELECT query string to execute. Must be a valid SQL statement
                    following standard SQL syntax conventions.

    Returns:
        Dict[str, Any]: Query execution results.
    """
    # tool_result marks a failed dispatch `isError: true` while keeping the structured payload
    # (required — see docs/extending.md § Signalling failure). The *_impl keeps returning a plain
    # dict, so tests and the response contract are unchanged.
    return tool_result(run_query_impl(sqlSelectQuery=query, config=config_from_ctx(ctx)))
