"""Unit tests for the Colab handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitor.handlers.colab import handle_colab


def _mock_aiplatform_module(
    monkeypatch: pytest.MonkeyPatch,
    schedule_client: MagicMock,
    notebook_client: MagicMock,
) -> None:
    google = ModuleType("google")
    google.__path__ = []
    cloud = ModuleType("google.cloud")
    cloud.__path__ = []
    aiplatform_v1 = ModuleType("google.cloud.aiplatform_v1")

    aiplatform_v1.ScheduleServiceClient = MagicMock(return_value=schedule_client)
    aiplatform_v1.NotebookServiceClient = MagicMock(return_value=notebook_client)

    google.cloud = cloud
    cloud.aiplatform_v1 = aiplatform_v1

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.aiplatform_v1", aiplatform_v1)


@pytest.mark.unit
@pytest.mark.handler
class TestHandleColab:
    def test_cleanup_deletes_matching_colab_assets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        schedule_client = MagicMock()
        notebook_client = MagicMock()

        schedule_client.list_schedules.return_value = [
            SimpleNamespace(
                name="projects/test/locations/us-central1/schedules/ansible-test-sched",
                display_name="sched",
            )
        ]
        notebook_client.list_notebook_execution_jobs.return_value = [
            SimpleNamespace(
                name="projects/test/locations/us-central1/notebookExecutionJobs/job1",
                display_name="ansible-test-job",
            )
        ]
        notebook_client.list_notebook_runtimes.return_value = [
            SimpleNamespace(
                name="projects/test/locations/us-central1/notebookRuntimes/ansible-test-runtime",
                display_name="runtime",
            )
        ]
        notebook_client.list_notebook_runtime_templates.return_value = [
            SimpleNamespace(
                name="projects/test/locations/us-central1/notebookRuntimeTemplates/tmpl1",
                display_name="ansible-test-template",
            )
        ]

        op = MagicMock()
        op.result.return_value = None
        schedule_client.delete_schedule.return_value = op
        notebook_client.delete_notebook_execution_job.return_value = op
        notebook_client.delete_notebook_runtime.return_value = op
        notebook_client.delete_notebook_runtime_template.return_value = op

        _mock_aiplatform_module(monkeypatch, schedule_client, notebook_client)

        result = handle_colab("test-project", "us-central1", "ansible-test-", True)

        schedule_client.delete_schedule.assert_called_once()
        notebook_client.delete_notebook_execution_job.assert_called_once()
        notebook_client.delete_notebook_runtime.assert_called_once()
        notebook_client.delete_notebook_runtime_template.assert_called_once()
        assert len(result["found"]) == 4
        assert len(result["actions"]) == 4

    def test_schedule_list_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        schedule_client = MagicMock()
        notebook_client = MagicMock()
        schedule_client.list_schedules.side_effect = RuntimeError(
            "schedule list failed"
        )
        _mock_aiplatform_module(monkeypatch, schedule_client, notebook_client)

        with pytest.raises(RuntimeError, match="schedule list failed"):
            handle_colab("test-project", "us-central1", "ansible-test-", False)
