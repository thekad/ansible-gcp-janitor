"""Unit tests for the BigQuery handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from janitor.handlers.bigquery import handle_bigquery


def _mock_bigquery_module(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    bigquery = ModuleType("google.cloud.bigquery")
    bigquery.Client = MagicMock(return_value=client)
    google.cloud = cloud
    cloud.bigquery = bigquery

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery)


@pytest.mark.unit
@pytest.mark.handler
class TestHandleBigQuery:
    def test_dry_run_matches_dash_and_underscore_prefixes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_datasets.return_value = [
            SimpleNamespace(dataset_id="ansible-test-dash"),
            SimpleNamespace(dataset_id="ansible_test_underscore"),
            SimpleNamespace(dataset_id="prod_dataset"),
        ]
        _mock_bigquery_module(monkeypatch, client)

        result = handle_bigquery("test-project", "ansible-test-", False)

        assert result == {
            "found": [
                {"type": "bigquery.Dataset", "name": "ansible-test-dash"},
                {"type": "bigquery.Dataset", "name": "ansible_test_underscore"},
            ],
            "actions": [],
        }
        client.delete_dataset.assert_not_called()

    def test_cleanup_deletes_only_matching_datasets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_datasets.return_value = [
            SimpleNamespace(dataset_id="ansible_test_target"),
            SimpleNamespace(dataset_id="prod_dataset"),
        ]
        _mock_bigquery_module(monkeypatch, client)

        result = handle_bigquery("test-project", "ansible-test-", True)

        client.delete_dataset.assert_has_calls(
            [
                call(
                    "ansible_test_target",
                    delete_contents=True,
                    not_found_ok=True,
                )
            ]
        )
        assert result["actions"] == ["Deleted BigQuery Dataset: ansible_test_target"]

    def test_dataset_list_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_datasets.side_effect = RuntimeError("dataset list failed")
        _mock_bigquery_module(monkeypatch, client)

        result = handle_bigquery("test-project", "ansible-test-", False)

        assert result["found"] == []
        assert result["actions"] == [
            "[WARN] BigQuery dataset list failed: dataset list failed"
        ]
