import logging
from typing import Optional, Dict, Any, List
from fastmcp import Context
from fastmcp.tools import tool
from kx_mcp_core import tool_result
from kx_mcp_kdbai.utils.embeddings import get_provider
from kx_mcp_kdbai.utils.observe import call, record_result_rows
from kx_mcp_kdbai.utils.embeddings_helpers import get_embedding_config
from kx_mcp_kdbai.utils.kdbai import get_table, config_from_ctx
from kx_mcp_kdbai.utils.filters import parse_temporal_filters
from kx_mcp_kdbai.settings import KDBAIConfig
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Normalizes the result from query and search operations
def normalize_result(df: pd.DataFrame, table)-> Any:
    # Remove embedding columns if they exist
    if table.indexes:
        embedding_columns = {t['column'] for t in table.indexes}
        df = df.drop(columns=embedding_columns, errors='ignore')
    # serialize numpy ndarray type (emedding columns)
    df = df.map(lambda x: x.tolist() if isinstance(x, np.ndarray) else x)
    # convert timespan type (KDB time type)
    for col_name, col_type in df.dtypes.items():
        timespan_type = str(col_type).lower().startswith("timedelta")
        duration_type = str(col_type).lower().startswith("duration")
        if timespan_type or duration_type:
            df[col_name] = (pd.Timestamp("1970-01-01") + df[col_name]).dt.time
        # convert to dict
    return df.to_dict('records') if hasattr(df, 'to_dict') else df

def kdbai_query_data_impl(table_name: str,
                                database_name: Optional[str] = None,
                                filters: Optional[List[tuple]] = None,
                                sort_columns: Optional[List[str]] = None,
                                group_by: Optional[List[str]] = None,
                                aggs: Optional[Dict[str, Any]] = None,
                                limit: Optional[int] = None,
                                config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    cfg = config if config is not None else KDBAIConfig()
    try:
        if database_name is None:
            database_name = cfg.database_name

        table = get_table(table_name, database_name, cfg)

        # Build query parameters efficiently
        query_params = {k: v for k, v in {
            'filter': parse_temporal_filters(filters,table.schema),
            'sort_columns': sort_columns,
            'group_by': group_by,
            'aggs': aggs,
            'limit': limit
        }.items() if v is not None}

        result = call("query", table.query, **query_params)
        result = normalize_result(result, table)
        record_result_rows(len(result))
        return {
            "status": "success",
            "database": database_name,
            "table": table_name,
            "recordsCount": len(result),
            "records": result
        }

    except Exception as e:
        logger.error(f"Error executing query on table {table_name}: {e}")
        return {
            "status": "error",
            "message": str(e),
            "database": database_name,
            "table": table_name
        }


async def kdbai_similarity_search_impl( table_name: str,
                                        query: str,
                                        vector_index_name: str,
                                        database_name: Optional[str] = None,
                                        n: Optional[int] = None,
                                        filters: Optional[List[tuple]] = None,
                                        sort_columns: Optional[List[str]] = None,
                                        group_by: Optional[List[str]] = None,
                                        aggs: Optional[Dict[str, Any]] = None,
                                        config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:

    cfg = config if config is not None else KDBAIConfig()
    try:
        if database_name is None:
            database_name = cfg.database_name
        if n is None:
            n = cfg.k

        embeddings_provider, embeddings_model, _, _ = get_embedding_config(database_name, table_name)
        assert embeddings_provider is not None and embeddings_model is not None, "table is missing embedding provider/model config"

        dense_provider = get_provider(embeddings_provider)
        query_vector = await dense_provider.dense_embed(query, embeddings_model)
        table = get_table(table_name, database_name, cfg)

        # Build search parameters efficiently
        search_params = {
            "vectors": {vector_index_name: [query_vector]},
            "n": int(n),
            **{k: v for k, v in {
                'filter': parse_temporal_filters(filters,table.schema),
                'sort_columns': sort_columns,
                'group_by': group_by,
                'aggs': aggs
            }.items() if v is not None}
        }

        result = call("search", table.search, **search_params)[0]
        result = normalize_result(result, table)
        record_result_rows(len(result))

        return {
            "status": "success",
            "database": database_name,
            "table": table_name,
            "recordsCount": len(result),
            "records": result
        }
    except Exception as e:
        logger.error(f"Error performing search on table {table_name}: {e}")
        return {
            "status": "error",
            "message": str(e),
            "database": database_name,
            "table": table_name,
        }


async def kdbai_hybrid_search_impl(table_name: str,
                                    query: str,
                                    vector_index_name: str,
                                    sparse_index_name: str,
                                    database_name: Optional[str] = None,
                                    n: Optional[int] = None,
                                    filters: Optional[List[tuple]] = None,
                                    sort_columns: Optional[List[str]] = None,
                                    group_by: Optional[List[str]] = None,
                                    aggs: Optional[Dict[str, Any]] = None,
                                    config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    cfg = config if config is not None else KDBAIConfig()
    try:
        if database_name is None:
            database_name = cfg.database_name
        if n is None:
            n = cfg.k

        table = get_table(table_name, database_name, cfg)

        embeddings_provider, embeddings_model, sparse_tokenizer_provider, sparse_tokenizer_model = get_embedding_config(database_name, table_name)
        assert embeddings_provider is not None and embeddings_model is not None, "table is missing embedding provider/model config"
        assert sparse_tokenizer_provider is not None and sparse_tokenizer_model is not None, "table is missing sparse tokenizer config"

        dense_provider = get_provider(embeddings_provider)
        sparse_provider = dense_provider if embeddings_provider==sparse_tokenizer_provider else  get_provider(sparse_tokenizer_provider)
        query_vector = await dense_provider.dense_embed(query, embeddings_model)
        query_sparse = await sparse_provider.sparse_embed(query, sparse_tokenizer_model)

        search_params = {
            "vectors": {
                vector_index_name: [query_vector],
                sparse_index_name: [query_sparse],
            },
            "n": int(n),
            "index_params": {
                vector_index_name: {"weight": cfg.vector_weight},
                sparse_index_name: {"weight": cfg.sparse_weight},
            },
            **{k: v for k, v in {
                'filter': parse_temporal_filters(filters,table.schema),
                'sort_columns': sort_columns,
                'group_by': group_by,
                'aggs': aggs
            }.items() if v is not None}
        }

        result = call("hybrid_search", table.search, **search_params)[0]
        result = normalize_result(result, table)
        record_result_rows(len(result))
        return {
            "status": "success",
            "database": database_name,
            "table": table_name,
            "recordsCount": len(result),
            "records": result
        }
    except Exception as e:
        logger.error(f"Error performing hybrid search on table {table_name}: {e}")
        return {
            "status": "error",
            "message": str(e),
            "database": database_name,
            "table": table_name,
        }


# Standalone @tool decorators (FastMCP 3.x native discovery): each attaches __fastmcp__ metadata
# and returns the function unchanged. Bare names — the container supplies the `kdbai` qualifier via
# mount(namespace="kdbai"), so the composed tools are `kdbai_query_data` / `kdbai_similarity_search`
# / `kdbai_hybrid_search` (not double-prefixed).
@tool(annotations={"readOnlyHint": True})
async def query_data(table_name: str,
                     ctx: Context,
                     database_name: Optional[str] = None,
                     filters: Optional[List[tuple]] = None,
                     sort_columns: Optional[List[str]] = None,
                     group_by: Optional[List[str]] = None,
                     aggs: Optional[Dict[str, Any]] = None,
                     limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Query data from a KDBAI table with support for filtering, sorting, grouping,limit and aggregation.
    It removes the embedding columns from the output.

    For query syntax and examples, see: file://kdbai/guidance/kdbai-operations

    Args:
        database_name: Name of the database containing the table.
        table_name: Name of the table to query
        filters: List of filter conditions as q/kdb+ parse tree (operator, filter column name, value)
        Examples:
            - Simple equality: ("=", "filter_column_name", "value")
            - Logical AND: [("<", "filter_column_name_1", "value"), (">", "filter_column_name_2", "value")]
        sort_columns: List of column names to sort by, e.g. '["price", "date"]'
        group_by: List of column names to group by, e.g. '["category"]'
        aggs: Dictionary of aggregation rules, e.g. '{"total": ["sum", "amount"]}'. It can use any KDB+ supported aggregation function like avg, max, sum etc.
        limit: String representation of maximum number of rows to return, e.g. "10"

    Returns:
        Dictionary containing query results or error message

    """
    return tool_result(kdbai_query_data_impl(
        table_name,
        database_name,
        filters,
        sort_columns,
        group_by,
        aggs,
        limit,
        config=config_from_ctx(ctx)
    ))


@tool(annotations={"readOnlyHint": True})
async def similarity_search(table_name: str,
                            query: str,
                            vector_index_name: str,
                            ctx: Context,
                            database_name: Optional[str] = None,
                            n: Optional[int] = None,
                            filters: Optional[List[tuple]] = None,
                            sort_columns: Optional[List[str]] = None,
                            group_by: Optional[List[str]] = None,
                            aggs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Perform vector similarity search on a KDB.AI table.
    For search syntax and examples, see: file://kdbai/guidance/kdbai-operations

    Args:
        table_name: Name of the table to search
        query: Text query to convert to vector and search
        vector_index_name: Name of the vector index to search against
        database (Optional[str], optional): Name of the database
        n (Optional[int], optional): Number of results to return
        filters (Optional[List[tuple]], optional): List of filter conditions as q/kdb+ parse tree (operator, filter column name, value).
            - Filters Examples:
             - Simple equality: ("=", "filter_column_name", "value")
             - Logical AND: [("<", "filter_column_name_1", "value"), (">", "filter_column_name_2", "value")]
        sort_columns: List of column names to sort by, e.g. '["price", "date"]'
        group_by: List of column names to group by, e.g. '["category"]'
        aggs: Dictionary of aggregation rules, e.g. '{"total": ["sum", "amount"]}'. It can use any KDB+ supported aggregation function like avg, max, sum etc.

    Returns:
        Dictionary containing search result.
    """
    return tool_result(await kdbai_similarity_search_impl(
        table_name,
        query,
        vector_index_name,
        database_name,
        n,
        filters,
        sort_columns,
        group_by,
        aggs,
        config=config_from_ctx(ctx)
    ))


@tool(annotations={"readOnlyHint": True})
async def hybrid_search(table_name: str,
                        query: str,
                        vector_index_name: str,
                        sparse_index_name: str,
                        ctx: Context,
                        database_name: Optional[str] = None,
                        n: Optional[int] = None,
                        filters: Optional[List[tuple]] = None,
                        sort_columns: Optional[List[str]] = None,
                        group_by: Optional[List[str]] = None,
                        aggs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Performs hybrid search on a KDB.AI table by combining vector and text(sparse) search on a KDB.AI table.
    For search syntax and examples, see: file://kdbai/guidance/kdbai-operations

    Args:
        table_name: Name of the table to search
        query: Text query for both vector and text or sparse search
        vector_index_name: Name of the vector index for similarity search
        sparse_index_name: Name of the sparse index for text search
        database (Optional[str], optional): Name of the database
        n (Optional[int], optional): Number of results to return
        filters (Optional[List[tuple]], optional): List of filter conditions as q/kdb+ parse tree (operator, filter column name, value).
            - Filters Examples:
             - Simple equality: ("=", "filter_column_name", "value")
             - Logical AND: [("<", "filter_column_name_1", "value"), (">", "filter_column_name_2", "value")]
        sort_columns: List of column names to sort by, e.g. '["price", "date"]'
        group_by: List of column names to group by, e.g. '["category"]'
        aggs: Dictionary of aggregation rules, e.g. '{"total": ["sum", "amount"]}'. It can use any KDB+ supported aggregation function like avg, max, sum etc.

    Returns:
        Dictionary containing hybrid search result.
    """
    return tool_result(await kdbai_hybrid_search_impl(
        table_name,
        query,
        vector_index_name,
        sparse_index_name,
        database_name,
        n,
        filters,
        sort_columns,
        group_by,
        aggs,
        config=config_from_ctx(ctx)
    ))
