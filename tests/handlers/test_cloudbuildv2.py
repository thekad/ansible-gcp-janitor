"""Unit tests for the Cloud Build v2 handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitor.handlers.cloudbuildv2 import handle_cloudbuildv2


def _mock_cloudbuild_v2_module(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    devtools = ModuleType("google.cloud.devtools")
    devtools.__path__ = []
    cloudbuild_v2 = ModuleType("google.cloud.devtools.cloudbuild_v2")
    cloudbuild_v2.RepositoryManagerClient = MagicMock(return_value=client)

    google.cloud = cloud
    cloud.devtools = devtools
    devtools.cloudbuild_v2 = cloudbuild_v2

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.devtools", devtools)
    monkeypatch.setitem(
        sys.modules, "google.cloud.devtools.cloudbuild_v2", cloudbuild_v2
    )


@pytest.mark.unit
@pytest.mark.handler
class TestHandleCloudBuildV2:
    def test_cleanup_deletes_repositories_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        conn_match = SimpleNamespace(
            name="projects/test/locations/us-central1/connections/ansible-test-conn"
        )
        conn_other = SimpleNamespace(
            name="projects/test/locations/us-central1/connections/prod-conn"
        )

        client.list_connections.return_value = [conn_match, conn_other]

        def list_repositories(parent: str):
            if parent == conn_match.name:
                return [
                    SimpleNamespace(name=f"{parent}/repositories/repo-a"),
                ]
            return [
                SimpleNamespace(name=f"{parent}/repositories/ansible-test-repo"),
                SimpleNamespace(name=f"{parent}/repositories/prod-repo"),
            ]

        client.list_repositories.side_effect = list_repositories

        operation = MagicMock()
        operation.result.return_value = None
        client.delete_repository.return_value = operation
        client.delete_connection.return_value = operation

        _mock_cloudbuild_v2_module(monkeypatch, client)

        result = handle_cloudbuildv2(
            "test-project", "us-central1", "ansible-test-", True
        )

        deleted_repo_names = [
            call.kwargs["name"] for call in client.delete_repository.call_args_list
        ]
        assert set(deleted_repo_names) == {
            f"{conn_match.name}/repositories/repo-a",
            f"{conn_other.name}/repositories/ansible-test-repo",
        }
        client.delete_connection.assert_called_once_with(name=conn_match.name)

        connection_action_idx = result["actions"].index(
            f"Deleted CloudBuild v2 Connection: {conn_match.name}"
        )
        assert all(
            result["actions"].index(action) < connection_action_idx
            for action in result["actions"]
            if action.startswith("Deleted CloudBuild v2 Repository:")
        )

    def test_connection_list_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_connections.side_effect = RuntimeError("connection list failed")
        _mock_cloudbuild_v2_module(monkeypatch, client)

        result = handle_cloudbuildv2(
            "test-project", "us-central1", "ansible-test-", False
        )

        assert result["found"] == []
        assert result["actions"] == [
            "[WARN] CloudBuild v2 list failed: connection list failed"
        ]
