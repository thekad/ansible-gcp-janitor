"""Unit tests for the Compute handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from janitor.handlers.compute import handle_compute


def _mock_compute_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    compute_v1 = ModuleType("google.cloud.compute_v1")

    class InstancesClient:
        def aggregated_list(self, project: str):
            return [
                (
                    "zones/us-central1-a",
                    SimpleNamespace(
                        instances=[
                            SimpleNamespace(name="ansible-test-instance"),
                            SimpleNamespace(name="prod-instance"),
                        ]
                    ),
                )
            ]

    class DisksClient:
        def aggregated_list(self, project: str):
            return [
                (
                    "zones/us-central1-a",
                    SimpleNamespace(
                        disks=[
                            SimpleNamespace(name="ansible-test-disk"),
                            SimpleNamespace(name="prod-disk"),
                        ]
                    ),
                )
            ]

    class FirewallsClient:
        def list(self, project: str):
            return [
                SimpleNamespace(name="ansible-test-firewall"),
                SimpleNamespace(name="prod-firewall"),
            ]

    class SubnetworksClient:
        def aggregated_list(self, project: str):
            return [
                (
                    "regions/us-central1",
                    SimpleNamespace(
                        subnetworks=[
                            SimpleNamespace(
                                name="ansible-test-subnet",
                                network="https://compute.googleapis.com/projects/test/global/networks/custom-net",
                            ),
                            SimpleNamespace(
                                name="prod-subnet",
                                network="https://compute.googleapis.com/projects/test/global/networks/custom-net",
                            ),
                        ]
                    ),
                )
            ]

    class AddressesClient:
        def aggregated_list(self, project: str):
            return [
                (
                    "regions/us-central1",
                    SimpleNamespace(
                        addresses=[
                            SimpleNamespace(name="ansible-test-regional-ip"),
                            SimpleNamespace(name="prod-regional-ip"),
                        ]
                    ),
                ),
                (
                    "global",
                    SimpleNamespace(
                        addresses=[
                            SimpleNamespace(name="ansible-test-global-ip"),
                            SimpleNamespace(name="prod-global-ip"),
                        ]
                    ),
                ),
            ]

    class GlobalAddressesClient:
        pass

    class NetworksClient:
        def list(self, project: str):
            return [
                SimpleNamespace(
                    name="shared-network",
                    self_link="https://compute.googleapis.com/projects/test/global/networks/shared-network",
                    auto_create_subnetworks=False,
                    peerings=[],
                )
            ]

    compute_v1.InstancesClient = InstancesClient
    compute_v1.DisksClient = DisksClient
    compute_v1.FirewallsClient = FirewallsClient
    compute_v1.SubnetworksClient = SubnetworksClient
    compute_v1.AddressesClient = AddressesClient
    compute_v1.GlobalAddressesClient = GlobalAddressesClient
    compute_v1.NetworksClient = NetworksClient

    google.cloud = cloud
    cloud.compute_v1 = compute_v1

    google_auth = ModuleType("google.auth")
    google_auth_httplib2 = ModuleType("google_auth_httplib2")
    httplib2 = ModuleType("httplib2")

    googleapiclient = ModuleType("googleapiclient")
    googleapiclient.__path__ = []
    discovery = ModuleType("googleapiclient.discovery")
    discovery.build = lambda *args, **kwargs: None
    googleapiclient.discovery = discovery

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.compute_v1", compute_v1)
    monkeypatch.setitem(sys.modules, "google.auth", google_auth)
    monkeypatch.setitem(sys.modules, "google_auth_httplib2", google_auth_httplib2)
    monkeypatch.setitem(sys.modules, "httplib2", httplib2)
    monkeypatch.setitem(sys.modules, "googleapiclient", googleapiclient)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)


@pytest.mark.unit
@pytest.mark.handler
class TestHandleCompute:
    def test_dry_run_finds_matching_compute_resources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_compute_modules(monkeypatch)

        result = handle_compute("test-project", "us-central1", "ansible-test-", False)

        found_types = {item["type"] for item in result["found"]}
        assert found_types == {
            "compute.Instance",
            "compute.Disk",
            "compute.Firewall",
            "compute.Subnetwork",
            "compute.Address",
        }
        assert result["actions"] == []
