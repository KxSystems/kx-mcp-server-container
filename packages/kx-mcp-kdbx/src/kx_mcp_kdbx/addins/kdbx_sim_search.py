import logging
from typing import Optional, Dict, Any
from fastmcp import Context
from fastmcp.tools import tool
from kx_mcp_core import tool_result
from kx_mcp_kdbx.settings import KDBConfig
from kx_mcp_kdbx.utils.kdbx import get_kdb_connection, config_from_ctx
from kx_mcp_kdbx.utils.denial import denial_response
from kx_mcp_kdbx.utils.embeddings import get_provider
from kx_mcp_kdbx.utils.observe import q
from kx_mcp_kdbx.utils.format_utils import normalize_search_result
from kx_mcp_kdbx.utils.embeddings_helpers import get_embedding_config

# These two tools require the KDB-X AI libraries (`.ai`). They are registered UNCONDITIONALLY at
# discovery time and tagged so the bundle can gate them per-instance: McpServer calls
# `mcp.disable(tags={"requires-ai-libs"})` when the pre-flight finds AI libs unavailable, which
# hides them from list_tools() (not merely blocks at call-time) — preserving the prior behavior of
# never advertising a tool the backend can't fulfil, while keeping the gate instance-safe.
AI_LIBS_TAG = "requires-ai-libs"


logger = logging.getLogger(__name__)


async def kdbx_similarity_search_impl(table_name: str,
                                        query: str,
                                        n: Optional[int] = None,
                                        config: Optional[KDBConfig] = None) -> Dict[str, Any]:

    try:
        cfg = config if config is not None else KDBConfig()
        if n is None:
            n = cfg.k

        embeddings_column, embeddings_provider, embeddings_model, _, _, _, _ = get_embedding_config(table_name)
        assert embeddings_provider is not None and embeddings_model is not None, "table is missing embedding provider/model config"

        dense_provider = get_provider(embeddings_provider)
        query_vector = await dense_provider.dense_embed(query, embeddings_model)

        # Build search parameters
        search_params = {
            "table" : table_name,
            "vcol"  : embeddings_column,
            "qvec"  : query_vector,
            "metric": cfg.metric,
            "n"     : int(n),
        }

        conn = get_kdb_connection(config)

        result = q(conn, "search", r'''{[args]
                            c:args`vcol;
                            $[(args`table) in .Q.pt;
                                [
                                res:raze{[d;args;tbl;c]
                                    vecs:?[tbl;enlist (=;.Q.pf;d);0b;(enlist c)!enlist c]c;
                                    if[not count vecs; :()];
                                    res:.ai.flat.search[vecs;args`qvec;args`n;args`metric];
                                    res:res@\:iasc res[1];
                                    `dist xcols update dist:res[0] from ?[tbl;((=;.Q.pf;d);(in;`i;res[1]));0b;()]
                                }[;args;get args`table;c] each .Q.pv;
                                ![(args`n)#`dist xdesc res;();0b;enlist c]
                                ];
                                [
                                res:.ai.flat.search[?[args`table;();();c];args`qvec;args`n;args`metric];
                                ![(args`table) res[1];();0b;enlist c]
                                ]
                            ]}''', search_params)

        result = normalize_search_result(result.pd(), table_name)

        return {
            "status": "success",
            "table": table_name,
            "recordsCount": len(result),
            "records": result
        }
    except Exception as e:
        logger.error(f"Error performing search on table {table_name}: {e}")
        denied = denial_response(e)
        if denied is not None:
            denied["table"] = table_name
            return denied
        return {
            "status": "error",
            "message": str(e),
            "table": table_name,
        }


async def kdbx_hybrid_search_impl(table_name: str,
                                    query: str,
                                    n: Optional[int] = None,
                                    config: Optional[KDBConfig] = None) -> Dict[str, Any]:

    try:
        cfg = config if config is not None else KDBConfig()
        if n is None:
            n = cfg.k

        embeddings_column, embeddings_provider, embeddings_model, _, sparse_index_name, sparse_tokenizer_provider, sparse_tokenizer_model = get_embedding_config(table_name)
        assert embeddings_provider is not None and embeddings_model is not None, "table is missing embedding provider/model config"

        # Check if it has index
        if sparse_index_name is None:
            logger.info(f"Error performing hybrid search on table {table_name}: Missing sparse index")
            return {
                    "status": "error",
                    "message": "The requested table does not have sparse index",
                    "table": table_name,
                }

        # A sparse index implies its tokenizer config is populated; narrow for the type checker.
        assert sparse_tokenizer_provider is not None and sparse_tokenizer_model is not None, "table has a sparse index but is missing sparse tokenizer config"
        dense_provider = get_provider(embeddings_provider)
        sparse_provider = dense_provider if embeddings_provider==sparse_tokenizer_provider else  get_provider(sparse_tokenizer_provider)
        query_vector = await dense_provider.dense_embed(query, embeddings_model)
        query_sparse = await sparse_provider.sparse_embed(query, sparse_tokenizer_model)

        # Build search parameters
        search_params = {
            "table"  : table_name,
            "vcol"   : embeddings_column,
            "dense"  : query_vector,
            "index"  : sparse_index_name,
            "sparse" : query_sparse,
            "metric" : cfg.metric,
            "n"      : int(n),
        }

        conn = get_kdb_connection(config)

        result = q(conn, "hybrid_search", r'''{[args]
                            c:args`vcol;
                            $[(args`table) in .Q.pt;
                                [
                                rdense:raze{[d;args;tbl;c]
                                    vecs:?[tbl;enlist (=;.Q.pf;d);0b;(enlist c)!enlist c]c;
                                    if[not count vecs; :()];
                                    res:.ai.flat.search[vecs;args`dense;args`n;args`metric];
                                    res:res@\:iasc res[1];
                                    `dist`id xcols update dist:res[0],id:((0,neg[1]_sums[.Q.cn[tbl]])@.Q.pv?/:d)+res[1] from ?[tbl;((=;.Q.pf;d);(in;`i;res[1]));0b;()]
                                }[;args;get args`table;c] each .Q.pv;
                                rdense:![(args`n)#`dist xdesc rdense;();0b;enlist c];
                                rsparse:.ai.bm25.psearch[args`index;args`sparse;args`n;1.25e;0.75e;.Q.pv];
                                if[not count rsparse[0];:()];
                                ids:(args`n)#.ai.hybrid.rrf[(rdense`id;rsparse[1]);60];
                                .Q.ind[get args`table;ids]
                                ];
                                [
                                rdense:.ai.flat.search[?[args`table;();();c];args`dense;args`n;args`metric];
                                rsparse:.ai.bm25.search[get (args`index);args`sparse;args`n;1.25e;0.75e];
                                if[not count rsparse[0];:()];
                                res:(args`n)#.ai.hybrid.rrf[(rdense[1];rsparse[1]);60];
                                ![(args`table) res;();0b;enlist c]
                                ]
                            ]}''', search_params)
        
        if hasattr(result, '__len__') and len(result) == 0:
            logger.info(f"Hybrid search on table {table_name} returned no results - sparse search may have found no matches")
            return {
                "status": "success",
                "table": table_name,
                "recordsCount": 0,
                "records": [],
                "message": "No results found - the sparse search returned no matches for the query"
            }

        result = normalize_search_result(result.pd(), table_name)

        return {
            "status": "success",
            "table": table_name,
            "recordsCount": len(result),
            "records": result
        }
    except Exception as e:
        logger.error(f"Error performing search on table {table_name}: {e}")
        denied = denial_response(e)
        if denied is not None:
            denied["table"] = table_name
            return denied
        return {
            "status": "error",
            "message": str(e),
            "table": table_name,
        }
    

# Standalone @tool decorators (FastMCP 3.x native discovery): each attaches __fastmcp__ metadata
# and returns the function unchanged. Both carry the AI-libs tag so McpServer can hide them with
# mcp.disable(tags={AI_LIBS_TAG}) when the per-instance pre-flight reports AI libs unavailable.
@tool(tags={AI_LIBS_TAG}, annotations={"readOnlyHint": True})
async def similarity_search(table_name: str,
                            query: str,
                            n: Optional[int] = None,
                            ctx: Optional[Context] = None) -> Dict[str, Any]:
    """
    Perform vector similarity search on a KDB-X table.

    Args:
        table_name: Name of the table to search
        query: Text query to convert to vector and search
        n (Optional[int], optional): Number of results to return

    Returns:
        Dictionary containing search result.
    """
    results = await kdbx_similarity_search_impl(
        table_name,
        query,
        n,
        config=config_from_ctx(ctx),
    )
    return tool_result(results)


@tool(tags={AI_LIBS_TAG}, annotations={"readOnlyHint": True})
async def hybrid_search(table_name: str,
                        query: str,
                        n: Optional[int] = None,
                        ctx: Optional[Context] = None) -> Dict[str, Any]:
    """
    Performs hybrid search on a KDB-X table by combining both vector and text(sparse) search.

    Args:
        table_name: Name of the table to search
        query: Text query to convert to sparse and dense vectors and search
        n (Optional[int], optional): Number of results to return

    Returns:
        Dictionary containing search result.
    """
    results = await kdbx_hybrid_search_impl(
        table_name,
        query,
        n,
        config=config_from_ctx(ctx),
    )
    return tool_result(results)