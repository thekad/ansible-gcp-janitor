from __future__ import annotations

import logging
import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix, underscore_prefix


def handle_vertexai(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud import aiplatform_v1

    opts = {"api_endpoint": f"{region}-aiplatform.googleapis.com"}
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    # Some Vertex AI resources (Featurestore, FeatureOnlineStore, FeatureGroup)
    # require snake_case IDs — hyphens are not allowed. Ansible converts the
    # prefix to underscores when naming those resources, so we match both forms.
    vertex_prefix = underscore_prefix(prefix)

    def vertex_match(name: str, disp: str) -> bool:
        return (
            match_prefix(name, prefix)
            or match_prefix(disp, prefix)
            or match_prefix(name, vertex_prefix)
            or match_prefix(disp, vertex_prefix)
        )

    # Terminal job states that do not require cancellation before deletion
    _TERMINAL_JOB_STATES = {
        aiplatform_v1.JobState.JOB_STATE_SUCCEEDED,
        aiplatform_v1.JobState.JOB_STATE_FAILED,
        aiplatform_v1.JobState.JOB_STATE_CANCELLED,
        aiplatform_v1.JobState.JOB_STATE_PAUSED,
        aiplatform_v1.JobState.JOB_STATE_EXPIRED,
    }

    # Helper scanner macro
    def scan_and_delete(
        client_cls: type,
        list_method: str,
        delete_method: str,
        asset_type: str,
        delete_kwargs: dict[str, any] | None = None,
    ) -> None:
        try:
            client = client_cls(client_options=opts)
            tasks: list[T.Callable[[], None]] = []
            for item in getattr(client, list_method)(parent=parent):
                name = getattr(item, "name", "")
                disp = getattr(item, "display_name", "")
                if vertex_match(name, disp):
                    found.append({"type": f"vertexai.{asset_type}", "name": name})
                    if do_cleanup:

                        def _delete(name=name) -> None:
                            try:
                                kwargs = {"name": name}
                                if delete_kwargs:
                                    kwargs.update(delete_kwargs)
                                getattr(client, delete_method)(**kwargs).result()
                                actions.append(
                                    f"Deleted Vertex AI {asset_type}: {name}"
                                )
                            except Exception as de:
                                logging.warning(
                                    f"Could not delete Vertex AI {asset_type} {name}: {de}"
                                )

                        tasks.append(_delete)
            _run_concurrent(tasks)
        except Exception as e:
            msg = f"Vertex AI {asset_type} scan failed: {e}"
            logging.warning(msg)
            actions.append(f"[WARN] {msg}")

    def cancel_and_delete_jobs(
        client_cls: type,
        list_method: str,
        cancel_method: str,
        delete_method: str,
        asset_type: str,
    ) -> None:
        try:
            client = client_cls(client_options=opts)
            tasks: list[T.Callable[[], None]] = []
            for item in getattr(client, list_method)(parent=parent):
                name = getattr(item, "name", "")
                disp = getattr(item, "display_name", "")
                if not vertex_match(name, disp):
                    continue
                found.append({"type": f"vertexai.{asset_type}", "name": name})
                if do_cleanup:
                    state = getattr(item, "state", None)

                    def _cancel_and_delete(name=name, state=state) -> None:
                        if state not in _TERMINAL_JOB_STATES:
                            try:
                                getattr(client, cancel_method)(name=name)
                            except Exception as ce:
                                logging.warning(
                                    f"Could not cancel Vertex AI {asset_type} {name}: {ce}"
                                )
                        try:
                            getattr(client, delete_method)(name=name).result()
                            actions.append(f"Deleted Vertex AI {asset_type}: {name}")
                        except Exception as de:
                            logging.warning(
                                f"Could not delete Vertex AI {asset_type} {name}: {de}"
                            )

                    tasks.append(_cancel_and_delete)
            _run_concurrent(tasks)
        except Exception as e:
            msg = f"Vertex AI {asset_type} scan failed: {e}"
            logging.warning(msg)
            actions.append(f"[WARN] {msg}")

    # Models & Endpoints — four-phase coordinated cleanup:
    #   1. Scan models matching the prefix
    #   2. Undeploy each matched model from every endpoint that holds it
    #   3. Delete matched models (now safe — all deployments cleared)
    #   4. Scan endpoints matching the prefix by name/display_name and delete them
    try:
        model_client = aiplatform_v1.ModelServiceClient(client_options=opts)
        ep_client = aiplatform_v1.EndpointServiceClient(client_options=opts)

        # Phase 1: collect matched models
        matched_models = [
            m
            for m in model_client.list_models(parent=parent)
            if vertex_match(m.name, m.display_name)
        ]
        for m in matched_models:
            found.append({"type": "vertexai.Model", "name": m.name})

        if do_cleanup:
            # Phase 2: undeploy each matched model from all endpoints that hold it
            matched_model_ids = {m.name.split("/")[-1] for m in matched_models}
            undeploy_tasks: list[T.Callable[[], None]] = []
            for ep in ep_client.list_endpoints(parent=parent):
                for dm in ep.deployed_models or []:
                    if dm.model.split("/")[-1] in matched_model_ids:

                        def _undeploy(
                            ep_name=ep.name, dm_id=dm.id, model_name=dm.model
                        ) -> None:
                            try:
                                ep_client.undeploy_model(
                                    request=aiplatform_v1.UndeployModelRequest(
                                        endpoint=ep_name, deployed_model_id=dm_id
                                    )
                                ).result()
                            except Exception as ue:
                                logging.warning(
                                    f"Could not undeploy Model {model_name} from {ep_name}: {ue}"
                                )

                        undeploy_tasks.append(_undeploy)
            _run_concurrent(undeploy_tasks)

            # Phase 3: delete matched models
            model_tasks: list[T.Callable[[], None]] = []
            for m in matched_models:

                def _delete_model(model_name=m.name) -> None:
                    try:
                        model_client.delete_model(name=model_name).result()
                        actions.append(f"Deleted Vertex AI Model: {model_name}")
                    except Exception as de:
                        logging.warning(
                            f"Could not delete Vertex AI Model {model_name}: {de}"
                        )

                model_tasks.append(_delete_model)
            _run_concurrent(model_tasks)

    except Exception as e:
        msg = f"Vertex AI Model scan failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    # Phase 4: scan & delete matched endpoints
    try:
        ep_client = aiplatform_v1.EndpointServiceClient(client_options=opts)
        endpoint_tasks: list[T.Callable[[], None]] = []
        for ep in ep_client.list_endpoints(parent=parent):
            if vertex_match(ep.name, ep.display_name):
                found.append({"type": "vertexai.Endpoint", "name": ep.name})
                if do_cleanup:

                    def _delete_endpoint(ep=ep) -> None:
                        try:
                            for dm in ep.deployed_models or []:
                                ep_client.undeploy_model(
                                    request=aiplatform_v1.UndeployModelRequest(
                                        endpoint=ep.name, deployed_model_id=dm.id
                                    )
                                ).result()
                            ep_client.delete_endpoint(name=ep.name).result()
                            actions.append(f"Deleted Vertex AI Endpoint: {ep.name}")
                        except Exception as de:
                            logging.warning(
                                f"Could not delete Vertex AI Endpoint {ep.name}: {de}"
                            )

                    endpoint_tasks.append(_delete_endpoint)
        _run_concurrent(endpoint_tasks)
    except Exception as e:
        msg = f"Vertex AI Endpoint scan failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    # Jobs — cancel if non-terminal, then delete
    cancel_and_delete_jobs(
        aiplatform_v1.PipelineServiceClient,
        "list_pipeline_jobs",
        "cancel_pipeline_job",
        "delete_pipeline_job",
        "PipelineJob",
    )
    cancel_and_delete_jobs(
        aiplatform_v1.PipelineServiceClient,
        "list_training_pipelines",
        "cancel_training_pipeline",
        "delete_training_pipeline",
        "TrainingPipeline",
    )
    cancel_and_delete_jobs(
        aiplatform_v1.JobServiceClient,
        "list_batch_prediction_jobs",
        "cancel_batch_prediction_job",
        "delete_batch_prediction_job",
        "BatchPredictionJob",
    )
    cancel_and_delete_jobs(
        aiplatform_v1.JobServiceClient,
        "list_custom_jobs",
        "cancel_custom_job",
        "delete_custom_job",
        "CustomJob",
    )
    cancel_and_delete_jobs(
        aiplatform_v1.JobServiceClient,
        "list_hyperparameter_tuning_jobs",
        "cancel_hyperparameter_tuning_job",
        "delete_hyperparameter_tuning_job",
        "HyperparameterTuningJob",
    )

    # Generic Vertex AI Resources
    scan_and_delete(
        aiplatform_v1.DatasetServiceClient, "list_datasets", "delete_dataset", "Dataset"
    )
    scan_and_delete(
        aiplatform_v1.DeploymentResourcePoolServiceClient,
        "list_deployment_resource_pools",
        "delete_deployment_resource_pool",
        "DeploymentResourcePool",
    )
    # IndexEndpoints must have all deployed indexes undeployed before deletion,
    # and IndexEndpoints must be deleted before the backing Indexes.
    try:
        ie_client = aiplatform_v1.IndexEndpointServiceClient(client_options=opts)
        index_endpoint_tasks: list[T.Callable[[], None]] = []
        for ie in ie_client.list_index_endpoints(parent=parent):
            if vertex_match(ie.name, ie.display_name):
                found.append({"type": "vertexai.IndexEndpoint", "name": ie.name})
                if do_cleanup:

                    def _delete_index_endpoint(ie=ie) -> None:
                        try:
                            for di in ie.deployed_indexes or []:
                                ie_client.undeploy_index(
                                    request=aiplatform_v1.UndeployIndexRequest(
                                        index_endpoint=ie.name, deployed_index_id=di.id
                                    )
                                ).result()
                            ie_client.delete_index_endpoint(name=ie.name).result()
                            actions.append(
                                f"Deleted Vertex AI IndexEndpoint: {ie.name}"
                            )
                        except Exception as de:
                            logging.warning(
                                f"Could not delete Vertex AI IndexEndpoint {ie.name}: {de}"
                            )

                    index_endpoint_tasks.append(_delete_index_endpoint)
        _run_concurrent(index_endpoint_tasks)
    except Exception as e:
        msg = f"Vertex AI IndexEndpoint scan failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    scan_and_delete(
        aiplatform_v1.IndexServiceClient, "list_indexes", "delete_index", "Index"
    )

    # MetadataStores automatically create child resources (artifacts, contexts,
    # executions) as ML lineage records. These remain even after underlying
    # resources are deleted and must be cleaned up before the store itself can
    # be deleted.
    try:
        metadata_client = aiplatform_v1.MetadataServiceClient(client_options=opts)
        for store in metadata_client.list_metadata_stores(parent=parent):
            if not vertex_match(store.name, getattr(store, "display_name", "")):
                continue

            # Delete all artifacts from this matching store
            artifact_tasks: list[T.Callable[[], None]] = []
            for artifact in metadata_client.list_artifacts(parent=store.name):
                found.append({"type": "vertexai.Artifact", "name": artifact.name})
                if do_cleanup:

                    def _delete_artifact(name=artifact.name) -> None:
                        try:
                            metadata_client.delete_artifact(name=name).result()
                            actions.append(f"Deleted Vertex AI Artifact: {name}")
                        except Exception as de:
                            logging.warning(f"Could not delete Artifact {name}: {de}")

                    artifact_tasks.append(_delete_artifact)
            _run_concurrent(artifact_tasks)

            # Delete all contexts from this matching store
            context_tasks: list[T.Callable[[], None]] = []
            for context in metadata_client.list_contexts(parent=store.name):
                found.append({"type": "vertexai.Context", "name": context.name})
                if do_cleanup:

                    def _delete_context(name=context.name) -> None:
                        try:
                            metadata_client.delete_context(name=name).result()
                            actions.append(f"Deleted Vertex AI Context: {name}")
                        except Exception as de:
                            logging.warning(f"Could not delete Context {name}: {de}")

                    context_tasks.append(_delete_context)
            _run_concurrent(context_tasks)

            # Delete all executions from this matching store
            execution_tasks: list[T.Callable[[], None]] = []
            for execution in metadata_client.list_executions(parent=store.name):
                found.append({"type": "vertexai.Execution", "name": execution.name})
                if do_cleanup:

                    def _delete_execution(name=execution.name) -> None:
                        try:
                            metadata_client.delete_execution(name=name).result()
                            actions.append(f"Deleted Vertex AI Execution: {name}")
                        except Exception as de:
                            logging.warning(f"Could not delete Execution {name}: {de}")

                    execution_tasks.append(_delete_execution)
            _run_concurrent(execution_tasks)

    except Exception as e:
        msg = f"Vertex AI MetadataStore children cleanup failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    scan_and_delete(
        aiplatform_v1.MetadataServiceClient,
        "list_metadata_stores",
        "delete_metadata_store",
        "MetadataStore",
    )
    scan_and_delete(
        aiplatform_v1.ReasoningEngineServiceClient,
        "list_reasoning_engines",
        "delete_reasoning_engine",
        "ReasoningEngine",
    )
    scan_and_delete(
        aiplatform_v1.TensorboardServiceClient,
        "list_tensorboards",
        "delete_tensorboard",
        "Tensorboard",
    )

    # Featurestore, FeatureOnlineStore & FeatureGroup
    scan_and_delete(
        aiplatform_v1.FeaturestoreServiceClient,
        "list_featurestores",
        "delete_featurestore",
        "Featurestore",
        delete_kwargs={"force": True},
    )
    scan_and_delete(
        aiplatform_v1.FeatureOnlineStoreAdminServiceClient,
        "list_feature_online_stores",
        "delete_feature_online_store",
        "FeatureOnlineStore",
        delete_kwargs={"force": True},
    )
    scan_and_delete(
        aiplatform_v1.FeatureRegistryServiceClient,
        "list_feature_groups",
        "delete_feature_group",
        "FeatureGroup",
        delete_kwargs={"force": True},
    )

    return {"found": found, "actions": actions}
