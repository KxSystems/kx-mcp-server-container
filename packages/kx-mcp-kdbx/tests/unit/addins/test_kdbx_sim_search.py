"""Tests for kdbx_sim_search.py."""
import pytest
import pandas as pd
from unittest.mock import AsyncMock
from kx_mcp_kdbx.addins.kdbx_sim_search import kdbx_similarity_search_impl, kdbx_hybrid_search_impl
from kx_mcp_kdbx.settings import KDBConfig

class TestKdbxSimilaritySearchImpl:
    """Test the kdbx_similarity_search_impl function."""

    @pytest.mark.anyio
    async def test_successful_search(self, mocker):
        """Test successful similarity search execution."""
        # Arrange Data
        mock_sim_search_data = [
            {'id': 1, 'text': 'similar text', 'dist': 0.1},
            {'id': 2, 'text': 'another match', 'dist': 0.2}
        ]

        mock_get_embedding_config = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_sim_search.get_embedding_config',
            return_value=('vector_col', 'openai', 'text-embedding-ada-002', None, None, None, None)
        )

        mock_provider = AsyncMock()
        mock_provider.dense_embed.return_value = [0.1, 0.2, 0.3]

        mock_get_provider = mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.get_provider')
        mock_get_provider.return_value = mock_provider

        mock_sim_search_result = mocker.Mock()
        mock_sim_search_result.pd.return_value = pd.DataFrame(mock_sim_search_data)

        mock_conn = mocker.Mock()
        mock_conn.return_value = mock_sim_search_result

        mock_get_kdb_connection = mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.get_kdb_connection', return_value=mock_conn)

        # Mock normalize_search_result to return the data as-is
        mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.normalize_search_result', side_effect=lambda df, table: df.to_dict('records'))

        # Act
        result = await kdbx_similarity_search_impl("test_table", "search query", 5)

        # Assert
        assert result['status'] == 'success'
        assert result['table'] == 'test_table'
        assert result['recordsCount'] == 2
        assert len(result['records']) == 2
        assert result['records'][0]['text'] == 'similar text'

        mock_get_embedding_config.assert_called_once_with("test_table")
        mock_provider.dense_embed.assert_called_once_with("search query", "text-embedding-ada-002")
        mock_get_kdb_connection.assert_called_once()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "exception_msg,expected_msg_contains",
        [
            ("Embedding model not found", "Embedding model not found"),
            ("Table does not exist", "Table does not exist"),
            ("Connection failed", "Connection failed"),
        ],
    )
    async def test_error_handling(self, mocker, exception_msg, expected_msg_contains):
        """Test handling of various errors during search execution."""
        # Arrange Mocks
        mock_get_embedding_config = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_sim_search.get_embedding_config',
            side_effect=Exception(exception_msg)
        )

        # Act
        result = await kdbx_similarity_search_impl("test_table", "query", 5)

        # Assert
        assert result['status'] == 'error'
        assert expected_msg_contains in result['message']
        assert result['table'] == 'test_table'

        mock_get_embedding_config.assert_called_once_with("test_table")

    @pytest.mark.anyio
    async def test_search_parameters_construction(self, mocker):
        """Test that search parameters are correctly constructed."""
        # Arrange Data
        mock_sim_search_data = []

        # Arrange Mocks
        mock_get_embedding_config = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_sim_search.get_embedding_config',
            return_value=('embedding_col', 'provider', 'model', None, None, None, None)
        )

        mock_provider = AsyncMock()
        mock_provider.dense_embed.return_value = [1.0, 2.0, 3.0]

        mock_get_provider = mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.get_provider')
        mock_get_provider.return_value = mock_provider

        mock_sim_search_result = mocker.Mock()
        mock_sim_search_result.pd.return_value = pd.DataFrame(mock_sim_search_data)

        mock_conn = mocker.Mock()
        mock_conn.return_value = mock_sim_search_result

        mock_get_kdb_connection = mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.get_kdb_connection', return_value=mock_conn)

        # Mock normalize_search_result to return empty list
        mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.normalize_search_result', return_value=[])

        # Act — pass an explicit per-instance config; its metric must flow into search_params
        # (this is the instance-safety threading, not a module global).
        await kdbx_similarity_search_impl("my_table", "test query", 15, config=KDBConfig(metric='euclidean'))

        # Assert - Check the call was made with correct parameters
        assert mock_conn.call_count == 1
        call_args = mock_conn.call_args
        
        # The first argument is the q function, second is the search_params dict
        search_params = call_args[0][1]
        assert search_params['table'] == 'my_table'
        assert search_params['vcol'] == 'embedding_col'
        assert search_params['qvec'] == [1.0, 2.0, 3.0]
        assert search_params['metric'] == 'euclidean'
        assert search_params['n'] == 15

        mock_get_embedding_config.assert_called_once_with("my_table")
        mock_provider.dense_embed.assert_called_once_with("test query", "model")
        mock_get_kdb_connection.assert_called_once()



class TestKdbxHybridSearchImpl:
    """Test the kdbx_hybrid_search_impl function."""

    @pytest.mark.anyio
    async def test_successful_hybrid_search(self, mocker):
        """Test successful hybrid search execution."""
        mock_hybrid_search_data = [
            {'id': 1, 'text': 'similar text', 'score': 0.9},
            {'id': 2, 'text': 'another match', 'score': 0.8}
        ]

        mock_get_embedding_config = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_sim_search.get_embedding_config',
            return_value=('vector_col', 'openai', 'text-embedding-ada-002', 'sparse_col', 'test_index', 'sentence_transformers', 'all-MiniLM-L12-v2')
        )

        mock_dense_provider = AsyncMock()
        mock_dense_provider.dense_embed.return_value = [0.1, 0.2, 0.3]
        
        mock_sparse_provider = AsyncMock()
        mock_sparse_provider.sparse_embed.return_value = {1: 2, 5: 1, 10: 3}

        mock_get_provider = mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.get_provider')
        mock_get_provider.return_value = mock_dense_provider

        mock_hybrid_search_result = mocker.Mock()
        mock_hybrid_search_result.pd.return_value = pd.DataFrame(mock_hybrid_search_data)

        mock_conn = mocker.Mock()
        mock_conn.return_value = mock_hybrid_search_result

        mock_get_kdb_connection = mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.get_kdb_connection', return_value=mock_conn)

        # Mock normalize_search_result to return the data as-is
        mocker.patch('kx_mcp_kdbx.addins.kdbx_sim_search.normalize_search_result', side_effect=lambda df, table: df.to_dict('records'))

        result = await kdbx_hybrid_search_impl("test_table", "search query", 5)

        assert result['status'] == 'success'
        assert result['table'] == 'test_table'
        assert result['recordsCount'] == 2
        assert len(result['records']) == 2
        assert result['records'][0]['text'] == 'similar text'

        mock_get_embedding_config.assert_called_once_with("test_table")
        mock_dense_provider.dense_embed.assert_called_once_with("search query", "text-embedding-ada-002")
        mock_dense_provider.sparse_embed.assert_called_once_with("search query", "all-MiniLM-L12-v2")
        mock_get_kdb_connection.assert_called_once()

    @pytest.mark.anyio
    async def test_hybrid_search_missing_sparse_index(self, mocker):
        """Test hybrid search returns error when sparse index is missing."""
        mock_get_embedding_config = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_sim_search.get_embedding_config',
            return_value=('vector_col', 'openai', 'text-embedding-ada-002', None, None, None, None)
        )

        # Act
        result = await kdbx_hybrid_search_impl("test_table", "query", 5)

        # Assert
        assert result['status'] == 'error'
        assert 'does not have sparse index' in result['message']
        assert result['table'] == 'test_table'

        mock_get_embedding_config.assert_called_once_with("test_table")

    @pytest.mark.anyio
    async def test_hybrid_search_error_handling(self, mocker):
        """Test handling of errors during hybrid search execution."""
        mock_get_embedding_config = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_sim_search.get_embedding_config',
            side_effect=Exception("Config not found")
        )

        result = await kdbx_hybrid_search_impl("test_table", "query", 5)

        assert result['status'] == 'error'
        assert 'Config not found' in result['message']
        assert result['table'] == 'test_table'

        mock_get_embedding_config.assert_called_once_with("test_table")