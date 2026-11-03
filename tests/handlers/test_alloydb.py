"""Unit tests for the AlloyDB handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitor.handlers.alloydb import handle_alloydb


def _mock_alloydb_module(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    alloydb_v1 = ModuleType("google.cloud.alloydb_v1")

    class DeleteClusterRequest:
        def __init__(self, name: str, force: bool) -> None:
            self.name = name
            self.force = force

    alloydb_v1.AlloyDBAdminClient = MagicMock(return_value=client)
    alloydb_v1.DeleteClusterRequest = DeleteClusterRequest

    google.cloud = cloud
    cloud.alloydb_v1 = alloydb_v1

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.alloydb_v1", alloydb_v1)


@pytest.mark.unit
@pytest.mark.handler
class TestHandleAlloydb:
    def test_cleanup_deletes_backups_instances_then_cluster(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()

        backup = SimpleNamespace(
            name="projects/test/locations/us-central1/backups/ansible-test-backup"
        )
        cluster = SimpleNamespace(
            name="projects/test/locations/us-central1/clusters/ansible-test-cluster",
            display_name="ansible-test-cluster",
        )
        instance = SimpleNamespace(
            name="projects/test/locations/us-central1/clusters/ansible-test-cluster/instances/ansible-test-instance"
        )

        client.list_backups.return_value = [backup]
        client.list_clusters.return_value = [cluster]
        client.list_instances.return_value = [instance]

        operation = MagicMock()
        operation.result.return_value = None
        client.delete_backup.return_value = operation
        client.delete_instance.return_value = operation
        client.delete_cluster.return_value = operation

        _mock_alloydb_module(monkeypatch, client)

        result = handle_alloydb("test-project", "us-central1", "ansible-test-", True)

        client.delete_backup.assert_called_once_with(name=backup.name)
        client.delete_instance.assert_called_once_with(name=instance.name)
        client.delete_cluster.assert_called_once()
        cluster_request = client.delete_cluster.call_args.kwargs["request"]
        assert cluster_request.name == cluster.name
        assert cluster_request.force is True

        assert any("Deleted AlloyDB Backup" in action for action in result["actions"])
        assert any("Deleted AlloyDB Instance" in action for action in result["actions"])
        assert any("Deleted AlloyDB Cluster" in action for action in result["actions"])

    def test_backup_list_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_backups.side_effect = RuntimeError("backup list failed")
        client.list_clusters.return_value = []
        _mock_alloydb_module(monkeypatch, client)

        result = handle_alloydb("test-project", "us-central1", "ansible-test-", False)

        assert result["found"] == []
        assert result["actions"] == [
            "[WARN] AlloyDB backup list failed: backup list failed"
        ]
