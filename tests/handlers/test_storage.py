"""Unit tests for the Storage handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitor.handlers.storage import handle_storage


def _mock_storage_module(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    storage = ModuleType("google.cloud.storage")
    storage.Client = MagicMock(return_value=client)
    google.cloud = cloud
    cloud.storage = storage

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)


@pytest.mark.unit
@pytest.mark.handler
class TestHandleStorage:
    def test_dry_run_lists_only_matching_buckets_without_deleting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_buckets.return_value = [
            SimpleNamespace(name="ansible-test-bucket"),
            SimpleNamespace(name="production-bucket"),
        ]
        _mock_storage_module(monkeypatch, client)

        result = handle_storage("test-project", "ansible-test-", False)

        assert result == {
            "found": [{"type": "storage.Bucket", "name": "ansible-test-bucket"}],
            "actions": [],
        }
        client.bucket.assert_not_called()

    def test_cleanup_deletes_matching_buckets_with_force(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        bucket = MagicMock()
        client.list_buckets.return_value = [
            SimpleNamespace(name="ansible-test-bucket"),
            SimpleNamespace(name="production-bucket"),
        ]
        client.bucket.return_value = bucket
        _mock_storage_module(monkeypatch, client)

        result = handle_storage("test-project", "ansible-test-", True)

        client.bucket.assert_called_once_with("ansible-test-bucket")
        bucket.delete.assert_called_once_with(force=True)
        assert result["actions"] == ["Deleted Storage Bucket: ansible-test-bucket"]

    def test_bucket_list_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_buckets.side_effect = RuntimeError("bucket list failed")
        _mock_storage_module(monkeypatch, client)

        result = handle_storage("test-project", "ansible-test-", False)

        assert result["found"] == []
        assert result["actions"] == [
            "[WARN] Storage bucket list failed: bucket list failed"
        ]
