"""Tests for embeddings_helpers.py."""
import pytest
import pandas as pd
from kx_mcp_kdbx.utils.embeddings_helpers import get_embedding_config


class TestGetEmbeddingConfig:
    """Test the get_embedding_config function."""

    @pytest.mark.parametrize(
        "table_name,csv_data,expected_result",
        [
            # Successful match - complete configuration
            (
                'users',
                pd.DataFrame({
                    'table': ['users'],
                    'embedding_column': ['user_vector'],
                    'embedding_provider': ['openai'],
                    'embedding_model': ['text-embedding-ada-002'],
                    'sparse_embedding_column': ['sparse_vector'],
                    'sparse_index_name': ['users_sparse_idx'],
                    'sparse_tokenizer_provider': ['sentence_transformers'],
                    'sparse_tokenizer_model': ['all-MiniLM-L12-v2']
                }),
                ['user_vector', 'openai', 'text-embedding-ada-002', 'sparse_vector', 'users_sparse_idx', 'sentence_transformers', 'all-MiniLM-L12-v2']
            ),
            # Successful match - with None values for sparse
            (
                'products',
                pd.DataFrame({
                    'table': ['products'],
                    'embedding_column': ['product_embedding'],
                    'embedding_provider': ['openai'],
                    'embedding_model': ['text-embedding-3-small'],
                    'sparse_embedding_column': [None],
                    'sparse_index_name': [None],
                    'sparse_tokenizer_provider': [None],
                    'sparse_tokenizer_model': [None]
                }),
                ['product_embedding', 'openai', 'text-embedding-3-small', None, None, None, None]
            ),
        ],
    )
    def test_embedding_config_success_scenarios(self, mocker, table_name, csv_data, expected_result):
        """Test successful embedding configuration retrieval."""
        # Arrange
        mock_config = mocker.patch('kx_mcp_kdbx.utils.embeddings_helpers.config')
        mock_config.embedding_csv_path = '/path/to/embeddings.csv'
        mock_get_csv_data = mocker.patch('kx_mcp_kdbx.utils.embeddings_helpers.get_csv_data', return_value=csv_data)

        # Act
        result = get_embedding_config(table_name)

        # Assert
        assert result == expected_result
        mock_get_csv_data.assert_called_once_with('/path/to/embeddings.csv')

    def test_embedding_config_no_match(self, mocker):
        """Test error when no configuration found for table."""
        # Arrange
        csv_data = pd.DataFrame({
            'table': ['users'],
            'embedding_column': ['user_vector'],
            'embedding_provider': ['openai'],
            'embedding_model': ['text-embedding-ada-002'],
            'sparse_embedding_column': [None],
            'sparse_index_name': [None],
            'sparse_tokenizer_provider': [None],
            'sparse_tokenizer_model': [None]
        })
        
        mock_config = mocker.patch('kx_mcp_kdbx.utils.embeddings_helpers.config')
        mock_config.embedding_csv_path = '/path/to/embeddings.csv'
        mocker.patch('kx_mcp_kdbx.utils.embeddings_helpers.get_csv_data', return_value=csv_data)

        # Act & Assert
        with pytest.raises(ValueError, match="No configuration found for table='nonexistent'"):
            get_embedding_config('nonexistent')

    def test_embedding_config_multiple_matches(self, mocker):
        """Test error when multiple configurations found for same table."""
        # Arrange
        csv_data = pd.DataFrame({
            'table': ['users', 'users'],
            'embedding_column': ['vector1', 'vector2'],
            'embedding_provider': ['openai', 'cohere'],
            'embedding_model': ['model1', 'model2'],
            'sparse_embedding_column': [None, None],
            'sparse_index_name': [None, None],
            'sparse_tokenizer_provider': [None, None],
            'sparse_tokenizer_model': [None, None]
        })
        
        mock_config = mocker.patch('kx_mcp_kdbx.utils.embeddings_helpers.config')
        mock_config.embedding_csv_path = '/path/to/embeddings.csv'
        mocker.patch('kx_mcp_kdbx.utils.embeddings_helpers.get_csv_data', return_value=csv_data)

        # Act & Assert
        with pytest.raises(ValueError, match="Multiple configurations found for table='users'"):
            get_embedding_config('users')
