# Testing the GCP Janitor

This document describes the test suite for ansible-gcp-janitor and how to run tests locally.

## Quick Start

### Install test dependencies

```bash
uv sync --extra dev
```

### Run all unit tests (recommended for local development)

```bash
uv run pytest tests/unit/ -v
# Or use the marker:
uv run pytest -m unit -v
```

### Run tests with coverage report

```bash
uv run pytest tests/unit/ --cov=janitor --cov=main --cov-report=html
```

This generates `htmlcov/index.html` with detailed coverage information.

### Run a specific test

```bash
uv run pytest tests/unit/test_core.py::TestMatchPrefix::test_simple_prefix_match -v
```

## Test Structure

```
tests/
  conftest.py                     # Pytest fixtures (Flask request mock, etc.)
  
  unit/                           # Fast unit tests (~0.1 seconds)
    __init__.py
    test_core.py                  # Tests for janitor/core.py utilities
    test_main.py                  # Tests for main.py HTTP handler
  
  handlers/                       # Handler test templates (for reference)
    __init__.py
```

## Test Categories

### Unit Tests (tests/unit/)

Fast, locally-run tests that test core logic without requiring GCP credentials.

**What's tested:**
- Core utilities: prefix matching, underscore conversion, thread pool execution
- HTTP handler: parameter parsing, handler orchestration, error resilience, scanner ordering
- Key behaviors: dry-run vs. cleanup mode, query param overrides, error handling
- Critical path: 100% coverage of deletion order and orchestration logic

**Why not handler tests here?**
Due to protobuf version conflicts in the Google Cloud SDKs, mocking GCP clients
in unit tests is problematic. See **Known Issues** section below.

**Run:**
```bash
uv run pytest tests/unit/ -v
# Or use the marker:
uv run pytest -m unit -v
```

### Integration Tests (tests/integration/)

No meaningful integration tests. This is a conscious decision because to have
an integration test we basically need to run the full ansible integration test.
We're restricting this test suite to unit tests.

## Fixtures (conftest.py)

### Shared Fixtures (tests/conftest.py)

- `mock_flask_request`: Mock Flask Request object for HTTP handler tests
- `project_id`: Test project ID string
- `region`: Test region string
- `prefix`: Test resource prefix string
- `mock_operation`: Mocked GCP long-running operation (with `.result()` method)
- `mock_gcp_response_page`: Mocked paginated API response

### Usage Example

```python
def test_handler(mock_flask_request, project_id, prefix):
    """Test with pre-configured fixtures."""
    mock_flask_request.args = {"prefix": prefix}
    result = scan_and_clean_resources(mock_flask_request)
```

## Coverage Goals

- **Unit tests (core + main)**: 100% coverage (currently achieved ✓)
- **Critical paths**: 100% coverage of:
  - Deletion ordering logic (verified via main.py tests)
  - Prefix matching and filtering (tested in test_core.py)
  - Error handling and exception paths (tested in test_main.py)
  - Dry-run vs. cleanup mode branching (tested in test_main.py)

## Troubleshooting

### "AssertionError: assert not sdk" when checking lazy imports

This means a Google Cloud SDK module was imported eagerly. Check that:
1. All Google Cloud imports are inside handler functions, not at module scope
2. Standard library imports (logging, typing, etc.) at module scope are OK
3. Test conftest.py doesn't import handlers at module scope

### Test execution times are slow

Unit tests should run in < 0.2 seconds. If slower:
- Check if any handlers are being imported (they shouldn't be)
- Verify fixtures aren't making network calls
- Use `pytest -v --durations=10` to identify slow tests

## Adding New Unit Tests

### Test structure for core utilities

```python
import pytest
from unittest.mock import MagicMock

@pytest.mark.unit
class TestMyUtility:
    """Tests for my_utility() function."""

    def test_basic_case(self):
        """Test the happy path."""
        result = my_utility("input")
        assert result == "expected"

    def test_error_case(self):
        """Test error handling."""
        with pytest.raises(ValueError):
            my_utility(None)
```

### Test structure for HTTP handler

```python
import json
import os
import pytest
from unittest.mock import patch

@pytest.mark.unit
class TestHttpHandler:
    """Tests for HTTP request handling."""

    @patch('main.handle_alloydb')
    def test_handler_success(self, mock_handler, mock_flask_request):
        """Test successful handler execution."""
        mock_handler.return_value = {"found": [], "actions": []}
        
        result, status_code, headers = http_handler(mock_flask_request)
        
        assert status_code == 200
        assert json.loads(result)["project"] == "test-project"
```

## References

- [Pytest Documentation](https://docs.pytest.org/)
- [unittest.mock Documentation](https://docs.python.org/3/library/unittest.mock.html)
- [Google Cloud Python Client Libraries](https://cloud.google.com/python/docs)
- [Protobuf Python API Breaking Changes](https://developers.google.com/protocol-buffers/docs/news/2022-05-06)
