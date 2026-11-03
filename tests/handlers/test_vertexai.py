"""Unit tests for the Vertex AI handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitor.handlers.vertexai import handle_vertexai


class _EmptyListClient:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _PipelineServiceClient(_EmptyListClient):
    def list_pipeline_jobs(self, parent: str):
        return []

    def list_training_pipelines(self, parent: str):
        return []


class _JobServiceClient(_EmptyListClient):
    def list_batch_prediction_jobs(self, parent: str):
        return []

    def list_custom_jobs(self, parent: str):
        return []

    def list_hyperparameter_tuning_jobs(self, parent: str):
        return []


class _EndpointServiceClient(_EmptyListClient):
    def list_endpoints(self, parent: str):
        return []


class _MetadataServiceClient(_EmptyListClient):
    def list_metadata_stores(self, parent: str):
        return []


class _SimpleGenericClient(_EmptyListClient):
    def list_datasets(self, parent: str):
        return []

    def list_deployment_resource_pools(self, parent: str):
        return []

    def list_indexes(self, parent: str):
        return []

    def list_reasoning_engines(self, parent: str):
        return []

    def list_tensorboards(self, parent: str):
        return []

    def list_featurestores(self, parent: str):
        return []

    def list_feature_online_stores(self, parent: str):
        return []

    def list_feature_groups(self, parent: str):
        return []


class _DatasetServiceClient(_SimpleGenericClient):
    def list_datasets(self, parent: str):
        return [
            SimpleNamespace(
                name=f"{parent}/datasets/ansible_test_dataset",
                display_name="dataset",
            ),
            SimpleNamespace(
                name=f"{parent}/datasets/prod_dataset", display_name="prod"
            ),
        ]


class _FailingDatasetServiceClient(_SimpleGenericClient):
    def list_datasets(self, parent: str):
        raise RuntimeError("dataset scan failed")


class _ModelServiceClient(_EmptyListClient):
    def list_models(self, parent: str):
        return []


class _IndexEndpointServiceClient(_EmptyListClient):
    def list_index_endpoints(self, parent: str):
        return []


@pytest.mark.unit
@pytest.mark.handler
class TestHandleVertexAi:
    def test_dry_run_matches_underscore_prefixed_resources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        google = ModuleType("google")
        google.__path__ = []
        cloud = ModuleType("google.cloud")
        cloud.__path__ = []
        aiplatform_v1 = ModuleType("google.cloud.aiplatform_v1")

        aiplatform_v1.JobState = SimpleNamespace(
            JOB_STATE_SUCCEEDED=1,
            JOB_STATE_FAILED=2,
            JOB_STATE_CANCELLED=3,
            JOB_STATE_PAUSED=4,
            JOB_STATE_EXPIRED=5,
        )

        aiplatform_v1.ModelServiceClient = _ModelServiceClient
        aiplatform_v1.EndpointServiceClient = _EndpointServiceClient
        aiplatform_v1.PipelineServiceClient = _PipelineServiceClient
        aiplatform_v1.JobServiceClient = _JobServiceClient
        aiplatform_v1.DatasetServiceClient = _DatasetServiceClient
        aiplatform_v1.DeploymentResourcePoolServiceClient = _SimpleGenericClient
        aiplatform_v1.IndexEndpointServiceClient = _IndexEndpointServiceClient
        aiplatform_v1.IndexServiceClient = _SimpleGenericClient
        aiplatform_v1.MetadataServiceClient = _MetadataServiceClient
        aiplatform_v1.ReasoningEngineServiceClient = _SimpleGenericClient
        aiplatform_v1.TensorboardServiceClient = _SimpleGenericClient
        aiplatform_v1.FeaturestoreServiceClient = _SimpleGenericClient
        aiplatform_v1.FeatureOnlineStoreAdminServiceClient = _SimpleGenericClient
        aiplatform_v1.FeatureRegistryServiceClient = _SimpleGenericClient

        google.cloud = cloud
        cloud.aiplatform_v1 = aiplatform_v1

        monkeypatch.setitem(sys.modules, "google", google)
        monkeypatch.setitem(sys.modules, "google.cloud", cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.aiplatform_v1", aiplatform_v1)

        result = handle_vertexai("test-project", "us-central1", "ansible-test-", False)

        assert result["found"] == [
            {
                "type": "vertexai.Dataset",
                "name": "projects/test-project/locations/us-central1/datasets/ansible_test_dataset",
            }
        ]
        assert result["actions"] == []

    def test_dataset_scan_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        google = ModuleType("google")
        google.__path__ = []
        cloud = ModuleType("google.cloud")
        cloud.__path__ = []
        aiplatform_v1 = ModuleType("google.cloud.aiplatform_v1")

        aiplatform_v1.JobState = SimpleNamespace(
            JOB_STATE_SUCCEEDED=1,
            JOB_STATE_FAILED=2,
            JOB_STATE_CANCELLED=3,
            JOB_STATE_PAUSED=4,
            JOB_STATE_EXPIRED=5,
        )

        aiplatform_v1.ModelServiceClient = _ModelServiceClient
        aiplatform_v1.EndpointServiceClient = _EndpointServiceClient
        aiplatform_v1.PipelineServiceClient = _PipelineServiceClient
        aiplatform_v1.JobServiceClient = _JobServiceClient
        aiplatform_v1.DatasetServiceClient = _FailingDatasetServiceClient
        aiplatform_v1.DeploymentResourcePoolServiceClient = _SimpleGenericClient
        aiplatform_v1.IndexEndpointServiceClient = _IndexEndpointServiceClient
        aiplatform_v1.IndexServiceClient = _SimpleGenericClient
        aiplatform_v1.MetadataServiceClient = _MetadataServiceClient
        aiplatform_v1.ReasoningEngineServiceClient = _SimpleGenericClient
        aiplatform_v1.TensorboardServiceClient = _SimpleGenericClient
        aiplatform_v1.FeaturestoreServiceClient = _SimpleGenericClient
        aiplatform_v1.FeatureOnlineStoreAdminServiceClient = _SimpleGenericClient
        aiplatform_v1.FeatureRegistryServiceClient = _SimpleGenericClient

        google.cloud = cloud
        cloud.aiplatform_v1 = aiplatform_v1

        monkeypatch.setitem(sys.modules, "google", google)
        monkeypatch.setitem(sys.modules, "google.cloud", cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.aiplatform_v1", aiplatform_v1)

        result = handle_vertexai("test-project", "us-central1", "ansible-test-", False)

        assert result["actions"] == [
            "[WARN] Vertex AI Dataset scan failed: dataset scan failed"
        ]
