"""Pytest configuration and shared fixtures for the test suite."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_flask_request():
    """Mock Flask Request object for testing HTTP handlers."""
    request = MagicMock()
    request.args = {}
    request.method = "GET"
    return request


@pytest.fixture
def project_id():
    """Test project ID."""
    return "test-project-123"


@pytest.fixture
def region():
    """Test region."""
    return "us-central1"


@pytest.fixture
def prefix():
    """Test resource prefix."""
    return "ansible-test-"


@pytest.fixture
def mock_operation():
    """Mock a GCP long-running operation."""
    operation = MagicMock()
    operation.result.return_value = None
    return operation


@pytest.fixture
def mock_gcp_response_page():
    """Mock a paginated GCP API response page."""
    page = MagicMock()
    page.next_page_token = None
    return page


def create_mock_client(name: str = "MockClient") -> MagicMock:
    """Factory for creating mock GCP client instances.

    Args:
        name: Display name for the mock (useful for debugging)

    Returns:
        MagicMock configured as a GCP client
    """
    client = MagicMock(name=name)
    return client


@pytest.fixture
def mock_all_handlers():
    """Factory fixture that returns a dict of all mocked handlers.

    Usage:
        def test_something(mock_all_handlers):
            handler_mocks = mock_all_handlers()
            # Returns dict with keys: alloydb, cloudbuild, cloudbuildv2,
            # colab, vertexai, storage, bigquery, pubsub, compute
    """

    def _create_mocks():
        handlers = {
            "alloydb": MagicMock(return_value={"found": [], "actions": []}),
            "cloudbuild": MagicMock(return_value={"found": [], "actions": []}),
            "cloudbuildv2": MagicMock(return_value={"found": [], "actions": []}),
            "colab": MagicMock(return_value={"found": [], "actions": []}),
            "vertexai": MagicMock(return_value={"found": [], "actions": []}),
            "storage": MagicMock(return_value={"found": [], "actions": []}),
            "bigquery": MagicMock(return_value={"found": [], "actions": []}),
            "pubsub": MagicMock(return_value={"found": [], "actions": []}),
            "compute": MagicMock(return_value={"found": [], "actions": []}),
        }
        return handlers

    return _create_mocks
