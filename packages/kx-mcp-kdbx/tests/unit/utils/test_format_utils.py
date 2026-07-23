"""Tests for format_utils.py."""
import numpy as np
import pandas as pd
from kx_mcp_kdbx.utils.format_utils import normalize_search_result, remove_vector_columns, format_data_for_display


class TestRemoveVectorColumns:
    """Test the remove_vector_columns function."""

    def test_remove_embedding_column(self, mocker):
        """Test removal of embedding column."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'vector_col': [[1, 2, 3], [4, 5, 6]],
            'name': ['Alice', 'Bob']
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config', 
                    return_value=['vector_col', 'provider', 'model', None, None, None, None])

        # Act
        result = remove_vector_columns(df, 'test_table')

        # Assert
        assert 'vector_col' not in result.columns
        assert 'id' in result.columns
        assert 'name' in result.columns

    def test_remove_sparse_column(self, mocker):
        """Test removal of sparse column."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'sparse_col': [[1, 2], [3, 4]],
            'name': ['Alice', 'Bob']
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config',
                    return_value=[None, None, None, 'sparse_col', None, None, None])

        # Act
        result = remove_vector_columns(df, 'test_table')

        # Assert
        assert 'sparse_col' not in result.columns
        assert 'id' in result.columns
        assert 'name' in result.columns

    def test_no_vector_columns_to_remove(self, mocker):
        """Test when no vector columns exist."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'name': ['Alice', 'Bob']
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config',
                    return_value=[None, None, None, None, None, None, None])

        # Act
        result = remove_vector_columns(df, 'test_table')

        # Assert
        assert list(result.columns) == ['id', 'name']


class TestFormatDataForDisplay:
    """Test the format_data_for_display function."""

    def test_format_with_dataframe_and_table_name(self, mocker):
        """Test formatting dataframe with table name removes vectors."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'vector': [[1, 2], [3, 4]],
            'name': ['Alice', 'Bob']
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config',
                    return_value=['vector', None, None, None, None, None, None])

        # Act
        result = format_data_for_display(df, 'test_table')

        # Assert
        assert 'vector' not in result
        assert 'id' in result

    def test_format_without_table_name(self):
        """Test formatting without table name keeps all columns."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'vector': [[1, 2], [3, 4]]
        })

        # Act
        result = format_data_for_display(df)

        # Assert
        assert 'vector' in result
        assert 'id' in result

    def test_format_non_dataframe(self):
        """Test formatting non-dataframe data."""
        # Arrange
        data = {'key': 'value'}

        # Act
        result = format_data_for_display(data)

        # Assert
        assert result == str(data)


class TestNormalizeSearchResult:
    """Test the normalize_search_result function."""

    def test_normalize_result_with_numpy_arrays(self, mocker):
        """Test normalization of numpy arrays in dataframe."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'vector': [np.array([1, 2, 3]), np.array([4, 5, 6])],
            'name': ['Alice', 'Bob']
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config',
                    return_value=[None, None, None, None, None, None, None])

        # Act
        result = normalize_search_result(df, 'test_table')

        # Assert
        assert len(result) == 2
        assert result[0]['vector'] == [1, 2, 3]
        assert result[1]['vector'] == [4, 5, 6]
        assert result[0]['name'] == 'Alice'

    def test_normalize_result_with_timedelta(self, mocker):
        """Test normalization of timedelta columns."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'duration': pd.to_timedelta(['1 days', '2 days'])
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config',
                    return_value=[None, None, None, None, None, None, None])

        # Act
        result = normalize_search_result(df, 'test_table')

        # Assert
        assert len(result) == 2
        assert 'duration' in result[0]
        assert 'duration' in result[1]

    def test_normalize_result_with_regular_data(self, mocker):
        """Test normalization with regular data types."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'name': ['Alice', 'Bob'],
            'score': [0.9, 0.8]
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config',
                    return_value=[None, None, None, None, None, None, None])

        # Act
        result = normalize_search_result(df, 'test_table')

        # Assert
        assert result == [
            {'id': 1, 'name': 'Alice', 'score': 0.9},
            {'id': 2, 'name': 'Bob', 'score': 0.8}
        ]

    def test_normalize_result_removes_vector_columns(self, mocker):
        """Test that vector columns are removed during normalization."""
        # Arrange
        df = pd.DataFrame({
            'id': [1, 2],
            'embedding': [np.array([1, 2]), np.array([3, 4])],
            'name': ['Alice', 'Bob']
        })
        mocker.patch('kx_mcp_kdbx.utils.format_utils.get_embedding_config',
                    return_value=['embedding', None, None, None, None, None, None])

        # Act
        result = normalize_search_result(df, 'test_table')

        # Assert
        assert len(result) == 2
        assert 'embedding' not in result[0]
        assert 'id' in result[0]
        assert 'name' in result[0]