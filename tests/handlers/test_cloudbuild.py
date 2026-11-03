"""Unit tests for the Cloud Build handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitor.handlers.cloudbuild import handle_cloudbuild


def _mock_cloudbuild_v1_module(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    devtools = ModuleType("google.cloud.devtools")
    devtools.__path__ = []
    cloudbuild_v1 = ModuleType("google.cloud.devtools.cloudbuild_v1")
    cloudbuild_v1.CloudBuildClient = MagicMock(return_value=client)

    google.cloud = cloud
    cloud.devtools = devtools
    devtools.cloudbuild_v1 = cloudbuild_v1

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.devtools", devtools)
    monkeypatch.setitem(
        sys.modules, "google.cloud.devtools.cloudbuild_v1", cloudbuild_v1
    )


@pytest.mark.unit
@pytest.mark.handler
class TestHandleCloudBuild:
    def test_cleanup_deletes_matching_triggers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_build_triggers.return_value = [
            SimpleNamespace(name="ansible-test-trigger", id="trg-1"),
            SimpleNamespace(name="prod-trigger", id="trg-2"),
        ]
        _mock_cloudbuild_v1_module(monkeypatch, client)

        result = handle_cloudbuild("test-project", "ansible-test-", True)

        client.delete_build_trigger.assert_called_once_with(
            project_id="test-project", trigger_id="trg-1"
        )
        assert result["found"] == [
            {
                "type": "cloudbuild.Trigger",
                "name": "ansible-test-trigger",
                "id": "trg-1",
            }
        ]
        assert result["actions"] == ["Deleted CloudBuild Trigger: ansible-test-trigger"]

    def test_trigger_list_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_build_triggers.side_effect = RuntimeError("trigger list failed")
        _mock_cloudbuild_v1_module(monkeypatch, client)

        result = handle_cloudbuild("test-project", "ansible-test-", False)

        assert result["found"] == []
        assert result["actions"] == [
            "[WARN] CloudBuild list failed: trigger list failed"
        ]
