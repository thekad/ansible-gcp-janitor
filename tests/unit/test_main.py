"""Unit tests for main.py HTTP handler."""

from __future__ import annotations

import json
import os
from unittest.mock import patch, call

import pytest

import main


@pytest.mark.unit
class TestScanAndCleanResources:
    """Tests for scan_and_clean_resources() HTTP handler."""

    def test_missing_project_id_returns_400(self, mock_flask_request, monkeypatch):
        """Test that missing TARGET_GCP_PROJECT_ID returns 400."""
        # Ensure no project ID is set
        monkeypatch.delenv("TARGET_GCP_PROJECT_ID", raising=False)

        result, status_code = main.scan_and_clean_resources(mock_flask_request)

        assert status_code == 400
        parsed = json.loads(result)
        assert "error" in parsed
        assert "TARGET_GCP_PROJECT_ID" in parsed["error"]

    @patch("main.handle_alloydb")
    @patch("main.handle_cloudbuild")
    @patch("main.handle_cloudbuildv2")
    @patch("main.handle_colab")
    @patch("main.handle_vertexai")
    @patch("main.handle_storage")
    @patch("main.handle_bigquery")
    @patch("main.handle_pubsub")
    @patch("main.handle_compute")
    def test_successful_dry_run(
        self,
        mock_compute,
        mock_pubsub,
        mock_bigquery,
        mock_storage,
        mock_vertexai,
        mock_colab,
        mock_cloudbuildv2,
        mock_cloudbuild,
        mock_alloydb,
        mock_flask_request,
        monkeypatch,
    ):
        """Test successful dry-run returns 200 and includes summary."""
        monkeypatch.setenv("TARGET_GCP_PROJECT_ID", "test-project")

        # Mock all handlers to return empty results
        for mock in [
            mock_compute,
            mock_pubsub,
            mock_bigquery,
            mock_storage,
            mock_vertexai,
            mock_colab,
            mock_cloudbuildv2,
            mock_cloudbuild,
            mock_alloydb,
        ]:
            mock.return_value = {"found": [], "actions": []}

        result, status_code, headers = main.scan_and_clean_resources(mock_flask_request)

        assert status_code == 200
        assert headers["Content-Type"] == "application/json"
        summary = json.loads(result)
        assert "mode" in summary
        assert "DRY-RUN" in summary["mode"]
        assert "project" in summary
        assert summary["project"] == "test-project"

    @patch("main.handle_alloydb")
    @patch("main.handle_cloudbuild")
    @patch("main.handle_cloudbuildv2")
    @patch("main.handle_colab")
    @patch("main.handle_vertexai")
    @patch("main.handle_storage")
    @patch("main.handle_bigquery")
    @patch("main.handle_pubsub")
    @patch("main.handle_compute")
    def test_cleanup_mode(
        self,
        mock_compute,
        mock_pubsub,
        mock_bigquery,
        mock_storage,
        mock_vertexai,
        mock_colab,
        mock_cloudbuildv2,
        mock_cloudbuild,
        mock_alloydb,
        mock_flask_request,
        monkeypatch,
    ):
        """Test cleanup mode sets do_cleanup=True."""
        monkeypatch.setenv("TARGET_GCP_PROJECT_ID", "test-project")

        # Mock all handlers
        for mock in [
            mock_compute,
            mock_pubsub,
            mock_bigquery,
            mock_storage,
            mock_vertexai,
            mock_colab,
            mock_cloudbuildv2,
            mock_cloudbuild,
            mock_alloydb,
        ]:
            mock.return_value = {"found": [], "actions": []}

        # Set cleanup mode via query arg
        mock_flask_request.args = {"do_cleanup": "true"}

        result, status_code, headers = main.scan_and_clean_resources(mock_flask_request)

        assert status_code == 200
        summary = json.loads(result)
        assert "ACTIVE DESTRUCTION" in summary["mode"]

    @patch("main.handle_alloydb")
    @patch("main.handle_cloudbuild")
    @patch("main.handle_cloudbuildv2")
    @patch("main.handle_colab")
    @patch("main.handle_vertexai")
    @patch("main.handle_storage")
    @patch("main.handle_bigquery")
    @patch("main.handle_pubsub")
    @patch("main.handle_compute")
    def test_query_params_override_env_vars(
        self,
        mock_compute,
        mock_pubsub,
        mock_bigquery,
        mock_storage,
        mock_vertexai,
        mock_colab,
        mock_cloudbuildv2,
        mock_cloudbuild,
        mock_alloydb,
        mock_flask_request,
        monkeypatch,
    ):
        """Test that query parameters override environment variables."""
        monkeypatch.setenv("TARGET_GCP_PROJECT_ID", "test-project")

        # Mock all handlers
        for mock in [
            mock_compute,
            mock_pubsub,
            mock_bigquery,
            mock_storage,
            mock_vertexai,
            mock_colab,
            mock_cloudbuildv2,
            mock_cloudbuild,
            mock_alloydb,
        ]:
            mock.return_value = {"found": [], "actions": []}

        # Set custom region and prefix via query args
        mock_flask_request.args = {
            "region": "europe-west1",
            "prefix": "custom-prefix-",
        }

        result, status_code, headers = main.scan_and_clean_resources(mock_flask_request)

        assert status_code == 200
        summary = json.loads(result)
        assert summary["region"] == "europe-west1"
        assert summary["prefix_filter"] == "custom-prefix-"

    @patch("main.handle_alloydb")
    @patch("main.handle_cloudbuild")
    @patch("main.handle_cloudbuildv2")
    @patch("main.handle_colab")
    @patch("main.handle_vertexai")
    @patch("main.handle_storage")
    @patch("main.handle_bigquery")
    @patch("main.handle_pubsub")
    @patch("main.handle_compute")
    def test_handler_failure_doesnt_stop_others(
        self,
        mock_compute,
        mock_pubsub,
        mock_bigquery,
        mock_storage,
        mock_vertexai,
        mock_colab,
        mock_cloudbuildv2,
        mock_cloudbuild,
        mock_alloydb,
        mock_flask_request,
        monkeypatch,
    ):
        """Test that one handler failure doesn't prevent other handlers from running."""
        monkeypatch.setenv("TARGET_GCP_PROJECT_ID", "test-project")

        # Make one handler fail
        mock_alloydb.side_effect = RuntimeError("API error")

        # Make other handlers succeed
        for mock in [
            mock_compute,
            mock_pubsub,
            mock_bigquery,
            mock_storage,
            mock_vertexai,
            mock_colab,
            mock_cloudbuildv2,
            mock_cloudbuild,
        ]:
            mock.return_value = {"found": [], "actions": []}

        result, status_code, headers = main.scan_and_clean_resources(mock_flask_request)

        # Should still return 200 despite one handler failing
        assert status_code == 200
        summary = json.loads(result)
        # Check that at least the successful handlers were called
        assert mock_cloudbuild.called

    @patch("main.handle_alloydb")
    @patch("main.handle_cloudbuild")
    @patch("main.handle_cloudbuildv2")
    @patch("main.handle_colab")
    @patch("main.handle_vertexai")
    @patch("main.handle_storage")
    @patch("main.handle_bigquery")
    @patch("main.handle_pubsub")
    @patch("main.handle_compute")
    def test_aggregates_found_resources(
        self,
        mock_compute,
        mock_pubsub,
        mock_bigquery,
        mock_storage,
        mock_vertexai,
        mock_colab,
        mock_cloudbuildv2,
        mock_cloudbuild,
        mock_alloydb,
        mock_flask_request,
        monkeypatch,
    ):
        """Test that found resources from all handlers are aggregated."""
        monkeypatch.setenv("TARGET_GCP_PROJECT_ID", "test-project")

        # Mock handlers to return some found resources
        mock_alloydb.return_value = {
            "found": [{"type": "AlloyDB Cluster", "id": "cluster-1"}],
            "actions": ["Deleted cluster-1"],
        }
        mock_storage.return_value = {
            "found": [{"type": "Bucket", "id": "bucket-1"}],
            "actions": ["Deleted bucket-1"],
        }

        # Other handlers return empty
        for mock in [
            mock_compute,
            mock_pubsub,
            mock_bigquery,
            mock_vertexai,
            mock_colab,
            mock_cloudbuildv2,
            mock_cloudbuild,
        ]:
            mock.return_value = {"found": [], "actions": []}

        result, status_code, headers = main.scan_and_clean_resources(mock_flask_request)

        assert status_code == 200
        summary = json.loads(result)
        assert len(summary["found_resources"]) == 2
        assert len(summary["actions_taken"]) == 2

    @patch("main.handle_alloydb")
    @patch("main.handle_cloudbuild")
    @patch("main.handle_cloudbuildv2")
    @patch("main.handle_colab")
    @patch("main.handle_vertexai")
    @patch("main.handle_storage")
    @patch("main.handle_bigquery")
    @patch("main.handle_pubsub")
    @patch("main.handle_compute")
    def test_scanners_execute_in_dependency_order(
        self,
        mock_compute,
        mock_pubsub,
        mock_bigquery,
        mock_storage,
        mock_vertexai,
        mock_colab,
        mock_cloudbuildv2,
        mock_cloudbuild,
        mock_alloydb,
        mock_flask_request,
        monkeypatch,
    ):
        """Test that handlers execute in strict dependency order.

        VPC consumers (alloydb, cloudbuild, etc.) must run before compute,
        because compute deletion can fail if VPC peerings/attachments are
        still active.
        """
        monkeypatch.setenv("TARGET_GCP_PROJECT_ID", "test-project")

        # Mock all handlers
        for mock in [
            mock_compute,
            mock_pubsub,
            mock_bigquery,
            mock_storage,
            mock_vertexai,
            mock_colab,
            mock_cloudbuildv2,
            mock_cloudbuild,
            mock_alloydb,
        ]:
            mock.return_value = {"found": [], "actions": []}

        result, status_code, headers = main.scan_and_clean_resources(mock_flask_request)

        assert status_code == 200

        # Verify exact call order: VPC consumers first, then storage/bigquery,
        # then pubsub, then compute last
        expected_order = [
            call("test-project", "us-central1", "ansible-test-", False),  # alloydb
            call("test-project", "ansible-test-", False),  # cloudbuild
            call("test-project", "us-central1", "ansible-test-", False),  # cloudbuildv2
            call("test-project", "us-central1", "ansible-test-", False),  # colab
            call("test-project", "us-central1", "ansible-test-", False),  # vertexai
            call("test-project", "ansible-test-", False),  # storage
            call("test-project", "ansible-test-", False),  # bigquery
            call("test-project", "ansible-test-", False),  # pubsub
            call("test-project", "us-central1", "ansible-test-", False),  # compute
        ]

        # Extract actual call order
        actual_calls = [
            mock_alloydb.call_args,
            mock_cloudbuild.call_args,
            mock_cloudbuildv2.call_args,
            mock_colab.call_args,
            mock_vertexai.call_args,
            mock_storage.call_args,
            mock_bigquery.call_args,
            mock_pubsub.call_args,
            mock_compute.call_args,
        ]

        assert actual_calls == expected_order
