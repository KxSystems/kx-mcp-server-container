"""Tests for embeddings.py."""
import pytest
from kx_mcp_kdbx.utils.embeddings import register_provider, get_provider, PROVIDER_REGISTRY


class TestProviderRegistry:
    """Test the provider registration system."""

    def test_register_provider_decorator(self):
        """Test that register_provider decorator adds providers to registry."""
        # Arrange
        initial_registry_size = len(PROVIDER_REGISTRY)

        # Act
        @register_provider("test_provider")
        class TestProvider:
            pass

        # Assert
        assert "test_provider" in PROVIDER_REGISTRY
        assert PROVIDER_REGISTRY["test_provider"] == TestProvider
        assert len(PROVIDER_REGISTRY) == initial_registry_size + 1

        # Cleanup
        del PROVIDER_REGISTRY["test_provider"]

    def test_get_provider_success(self, mocker):
        """Test successful provider retrieval."""
        # Arrange
        mock_provider_class = mocker.Mock()
        mock_instance = mocker.Mock()
        mock_provider_class.return_value = mock_instance

        PROVIDER_REGISTRY["test_provider"] = mock_provider_class

        # Act
        result = get_provider("test_provider")

        # Assert: the factory returns an instrumented view of the registered class's instance.
        mock_provider_class.assert_called_once()
        assert result._inner == mock_instance
        result.cleanup_embedding_model()
        mock_instance.cleanup_embedding_model.assert_called_once()


    def test_get_provider_unknown(self):
        """Test error when requesting unknown provider."""
        # Act & Assert
        with pytest.raises(ValueError, match="Unknown provider: nonexistent"):
            get_provider("nonexistent")