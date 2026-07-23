"""Impl tests for the data tools (query / similarity / hybrid), mocked KDB.AI client + embeddings."""

import asyncio

import pandas as pd

from kx_mcp_kdbai.addins import kdbai_data as mod


def _table(mocker, df):
    table = mocker.Mock()
    table.indexes = []          # normalize_result: no embedding columns to drop
    table.schema = []
    table.query.return_value = df
    table.search.return_value = [df]
    return table


def test_query_data_success(mocker):
    df = pd.DataFrame({"sym": ["AAPL", "MSFT"], "price": [1.0, 2.0]})
    mocker.patch.object(mod, "get_table", return_value=_table(mocker, df))

    out = asyncio.run(mod.kdbai_query_data_impl("trades", "mydb"))
    assert out["status"] == "success"
    assert out["recordsCount"] == 2
    assert out["records"][0] == {"sym": "AAPL", "price": 1.0}


def test_query_data_error(mocker):
    mocker.patch.object(mod, "get_table", side_effect=RuntimeError("bad query"))
    out = asyncio.run(mod.kdbai_query_data_impl("trades", "mydb"))
    assert out["status"] == "error"
    assert "bad query" in out["message"]


def test_similarity_search_success(mocker):
    df = pd.DataFrame({"doc": ["a", "b"]})
    mocker.patch.object(mod, "get_table", return_value=_table(mocker, df))
    mocker.patch.object(mod, "get_embedding_config", return_value=("st", "model", None, None))
    provider = mocker.Mock()
    provider.dense_embed = mocker.AsyncMock(return_value=[0.1, 0.2])
    mocker.patch.object(mod, "get_provider", return_value=provider)

    out = asyncio.run(mod.kdbai_similarity_search_impl("docs", "hello", "vec_idx", "mydb", n=2))
    assert out["status"] == "success"
    assert out["recordsCount"] == 2
    provider.dense_embed.assert_awaited_once()


def test_hybrid_search_uses_config_weights(mocker):
    df = pd.DataFrame({"doc": ["a"]})
    table = _table(mocker, df)
    mocker.patch.object(mod, "get_table", return_value=table)
    mocker.patch.object(mod, "get_embedding_config", return_value=("st", "model", "st", "tok"))
    provider = mocker.Mock()
    provider.dense_embed = mocker.AsyncMock(return_value=[0.1])
    provider.sparse_embed = mocker.AsyncMock(return_value={1: 0.5})
    mocker.patch.object(mod, "get_provider", return_value=provider)

    from kx_mcp_kdbai.settings import KDBAIConfig
    cfg = KDBAIConfig(vector_weight=0.6, sparse_weight=0.4)
    out = asyncio.run(mod.kdbai_hybrid_search_impl("docs", "q", "vec", "sparse", "mydb", n=1, config=cfg))

    assert out["status"] == "success"
    # the per-instance config weights are threaded into the search index_params
    _, kwargs = table.search.call_args
    assert kwargs["index_params"]["vec"]["weight"] == 0.6
    assert kwargs["index_params"]["sparse"]["weight"] == 0.4
