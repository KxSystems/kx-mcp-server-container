import logging
import pykx as kx
import json
from typing import Dict, Any
from fastmcp import Context
from fastmcp.tools import tool
from kx_mcp_kdbx.utils import kdbx as kdbx_utils
from kx_mcp_kdbx.utils.kdbx import get_kdb_connection, config_from_ctx
from kx_mcp_kdbx.utils.denial import denial_response, denial_from_decision
from kx_mcp_kdbx.utils.authz_kx_entitlements import consult_data_gate, derive_tables
from kx_mcp_core.auth import authorize

logger = logging.getLogger(__name__)
MAX_ROWS_RETURNED = 1000

# Capability check: the @authorize decorator runs inside the tool's own context, reads the inbound
# principal, and consults the configured authz strategy (KX_MCP_AUTHZ) over an MCP-semantic
# (action;resource) — here `query`/`kdbx.sql`. For kdb-x set KX_MCP_AUTHZ=kdbx_rbac to route this to
# the q `.kx.auth` policy engine over a *capability* grant set, distinct from the q-side data gate
# applied in the body via `.s.e`. Unset = route-only (allow). A capability deny raises
# AuthorizationDenied (a clean tool error) — distinct from the data-layer denial below, which the
# except-clause maps to a structured permission_denied.
@authorize(action="query", resource="kdbx.sql")
async def run_query_impl(sqlSelectQuery: str, config=None) -> Dict[str, Any]:
    try:
        dangerous_keywords = ['INSERT', 'DROP', 'DELETE', 'TRUNCATE', 'ALTER', 'CREATE']
        query_upper = sqlSelectQuery.upper().strip()

        for keyword in dangerous_keywords:
            if keyword in query_upper and not query_upper.startswith('SELECT'):
                raise ValueError(f"Query contains dangerous keyword: {keyword}")

        conn = get_kdb_connection(config)

        # Data gate (opt-in via KDBX_DB_DATA_GATE): explicitly consult the q-side entitlement gate
        # for the tables this query references, on the handle get_kdb_connection just bound
        # (in-tool, so require[] sees the principal — the bind-ordering constraint). Deny →
        # structured permission_denied; scope-down (partial entitlement) → the same envelope
        # carrying the entitled subset as guidance, since rewriting the SQL down to it is out of
        # scope — the agent re-scopes and re-issues. Full allow → run unchanged.
        cfg = config if config is not None else kdbx_utils.db_config
        if getattr(cfg, "data_gate", False):
            known_tables = [str(t) for t in conn('tables[]').py()]
            referenced = derive_tables(sqlSelectQuery, known_tables)
            if referenced:
                decision = consult_data_gate("read", referenced)
                if not decision.allowed or decision.obligations.get("denied"):
                    logger.info("Query denied at the data layer by the entitlements gate")
                    return denial_from_decision(decision)

        # below query gets kdbx table data back as json for correct conversion of different datatypes
        result = conn('{r:.s.e x;`rowCount`data!(count r;.j.j y sublist r)}', kx.CharVector(sqlSelectQuery), MAX_ROWS_RETURNED)
        total = int(result['rowCount'])
        if 0==total:
            return {"status": "success", "data": [], "message": "No rows returned"}
        # parse json result
        rows = json.loads(result['data'].py().decode('utf-8'))
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
@tool
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
    return await run_query_impl(sqlSelectQuery=query, config=config_from_ctx(ctx))
