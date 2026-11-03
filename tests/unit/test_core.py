"""Unit tests for janitor/core.py utilities."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from janitor.core import _run_concurrent, match_prefix, underscore_prefix


@pytest.mark.unit
class TestMatchPrefix:
    """Tests for match_prefix() function."""

    def test_simple_prefix_match(self):
        """Test basic prefix matching."""
        assert match_prefix("ansible-test-instance", "ansible-test-") is True

    def test_prefix_no_match(self):
        """Test name that doesn't match prefix."""
        assert match_prefix("prod-instance", "ansible-test-") is False

    def test_prefix_match_with_resource_path(self):
        """Test prefix matching with resource path (strips to short name)."""
        # Resource path: projects/123/zones/us-central1-a/instances/ansible-test-foo
        assert (
            match_prefix(
                "projects/123/zones/us-central1-a/instances/ansible-test-foo",
                "ansible-test-",
            )
            is True
        )

    def test_empty_name(self):
        """Test with empty resource name."""
        assert match_prefix("", "ansible-test-") is False

    def test_case_sensitive(self):
        """Test that matching is case-sensitive."""
        assert match_prefix("ANSIBLE-TEST-instance", "ansible-test-") is False

    def test_prefix_at_end_of_path(self):
        """Test extracting resource ID from complex paths."""
        long_path = "projects/my-project/zones/us-central1-a/instances/ansible-test-xyz"
        assert match_prefix(long_path, "ansible-test-") is True

    def test_prefix_not_match_partial_word(self):
        """Test that prefix doesn't match partial word boundaries."""
        # "ansible-test-" should not match "my-ansible-test-instance"
        assert match_prefix("my-ansible-test-instance", "ansible-test-") is False


@pytest.mark.unit
class TestUnderscorePrefix:
    """Tests for underscore_prefix() function."""

    def test_dash_to_underscore_conversion(self):
        """Test converting dashes to underscores."""
        assert underscore_prefix("ansible-test-") == "ansible_test_"

    def test_no_dashes(self):
        """Test string with no dashes."""
        assert underscore_prefix("ansibletest") == "ansibletest"

    def test_multiple_dashes(self):
        """Test string with multiple dashes."""
        assert (
            underscore_prefix("ansible-test-resource-name")
            == "ansible_test_resource_name"
        )

    def test_empty_string(self):
        """Test with empty string."""
        assert underscore_prefix("") == ""


@pytest.mark.unit
class TestRunConcurrent:
    """Tests for _run_concurrent() function."""

    def test_empty_task_list(self):
        """Test with no tasks."""
        _run_concurrent([])  # Should not raise

    def test_single_task(self):
        """Test with one task."""
        call_count = []

        def task():
            call_count.append(1)

        _run_concurrent([task])
        assert len(call_count) == 1

    def test_multiple_tasks_execute(self):
        """Test that multiple tasks all execute."""
        results = []

        def make_task(value):
            def task():
                results.append(value)

            return task

        tasks = [make_task(i) for i in range(5)]
        _run_concurrent(tasks)

        assert len(results) == 5
        assert set(results) == {0, 1, 2, 3, 4}

    def test_task_exception_propagates(self):
        """Test that exceptions in tasks are re-raised."""

        def failing_task():
            raise ValueError("Test error")

        def success_task():
            pass

        with pytest.raises(ValueError, match="Test error"):
            _run_concurrent([failing_task, success_task])

    def test_multiple_exception_handling(self):
        """Test that first exception is raised when multiple tasks fail."""

        def failing_task_1():
            raise ValueError("Error 1")

        def failing_task_2():
            raise RuntimeError("Error 2")

        with pytest.raises((ValueError, RuntimeError)):
            _run_concurrent([failing_task_1, failing_task_2])

    @patch("janitor.core.MAX_DELETE_WORKERS", 2)
    def test_respects_worker_limit(self):
        """Test that task execution respects MAX_DELETE_WORKERS."""
        with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_class:
            mock_pool = MagicMock()
            mock_pool_class.return_value.__enter__.return_value = mock_pool
            mock_pool.submit.return_value = MagicMock()

            mock_future = MagicMock()
            mock_future.result.return_value = None

            with patch("concurrent.futures.as_completed", return_value=[mock_future]):
                _run_concurrent([lambda: None, lambda: None])

            mock_pool_class.assert_called_once_with(max_workers=2)

    def test_task_side_effects_in_list_are_atomic(self):
        """Test that side effects (list appends) are thread-safe."""
        shared_list = []

        def make_task(value):
            def task():
                # Append is atomic in CPython, so multiple threads are safe
                shared_list.append(value)

            return task

        tasks = [make_task(i) for i in range(10)]
        _run_concurrent(tasks)

        assert len(shared_list) == 10
