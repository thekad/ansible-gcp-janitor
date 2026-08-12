from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, TypedDict

import functions_framework

if TYPE_CHECKING:
    from flask import Request


class HandlerResult(TypedDict):
    found: list[dict[str, str]]
    actions: list[str]


# Standard configuration defaults
DEFAULT_REGION = "us-central1"
DEFAULT_PREFIX = "ansible-test-"


def match_prefix(name: str, prefix: str) -> bool:
    """Checks if a resource name or display_name matches the filter prefix."""
    if not name:
        return False
    # Handle resource paths by taking the short resource ID
    short_name = name.split("/")[-1]
    return short_name.startswith(prefix)


def handle_compute(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud import compute_v1

    found, actions = [], []

    # 1. Instances
    inst_client = compute_v1.InstancesClient()
    for zone_str, response in inst_client.aggregated_list(project=project):
        if response.instances:
            zone = zone_str.split("/")[-1]
            if zone.startswith(region):
                for inst in response.instances:
                    if match_prefix(inst.name, prefix):
                        found.append(
                            {
                                "type": "compute.Instance",
                                "name": inst.name,
                                "zone": zone,
                            }
                        )
                        if do_cleanup:
                            inst_client.delete(
                                project=project, zone=zone, instance=inst.name
                            ).result()
                            actions.append(
                                f"Deleted Compute Instance: {inst.name} ({zone})"
                            )

    # 2. Disks
    disk_client = compute_v1.DisksClient()
    for zone_str, response in disk_client.aggregated_list(project=project):
        if response.disks:
            zone = zone_str.split("/")[-1]
            if zone.startswith(region):
                for disk in response.disks:
                    if match_prefix(disk.name, prefix):
                        found.append(
                            {"type": "compute.Disk", "name": disk.name, "zone": zone}
                        )
                        if do_cleanup:
                            disk_client.delete(
                                project=project, zone=zone, disk=disk.name
                            ).result()
                            actions.append(
                                f"Deleted Compute Disk: {disk.name} ({zone})"
                            )

    # 3. Firewall Rules (must go before networks)
    fw_client = compute_v1.FirewallsClient()
    for fw in fw_client.list(project=project):
        if match_prefix(fw.name, prefix):
            found.append({"type": "compute.Firewall", "name": fw.name})
            if do_cleanup:
                fw_client.delete(project=project, firewall=fw.name).result()
                actions.append(f"Deleted Compute Firewall: {fw.name}")

    # 4. Subnetworks (regional; must go before networks)
    # Build a set of network selfLinks whose subnets were auto-created so we
    # can skip them — GCP refuses to delete auto-created subnets individually.
    subnet_client = compute_v1.SubnetworksClient()
    _net_pre = compute_v1.NetworksClient()
    auto_subnet_networks = {
        net.self_link
        for net in _net_pre.list(project=project)
        if net.auto_create_subnetworks
    }
    for zone_str, response in subnet_client.aggregated_list(project=project):
        if response.subnetworks:
            subnet_region = zone_str.split("/")[-1]
            if subnet_region == region:
                for subnet in response.subnetworks:
                    if match_prefix(subnet.name, prefix):
                        if subnet.network in auto_subnet_networks:
                            logging.info(
                                f"Skipping auto-created subnetwork: {subnet.name}"
                            )
                            continue
                        found.append(
                            {
                                "type": "compute.Subnetwork",
                                "name": subnet.name,
                                "region": subnet_region,
                            }
                        )
                        if do_cleanup:
                            subnet_client.delete(
                                project=project,
                                region=subnet_region,
                                subnetwork=subnet.name,
                            ).result()
                            actions.append(f"Deleted Compute Subnetwork: {subnet.name}")

    # 5. Static/External IP Addresses (must go before networks)
    # aggregated_list returns regional addresses under "regions/{region}" keys
    # and global addresses under the "global" key — each requires a different
    # client and delete call.
    global_addr_client = compute_v1.GlobalAddressesClient()
    addr_client = compute_v1.AddressesClient()
    for zone_str, response in addr_client.aggregated_list(project=project):
        if not response.addresses:
            continue
        addr_scope = zone_str.split("/")[-1]
        for addr in response.addresses:
            if not match_prefix(addr.name, prefix):
                continue
            is_global = addr_scope == "global"
            if not is_global and addr_scope != region:
                continue
            found.append(
                {
                    "type": "compute.Address",
                    "name": addr.name,
                    "scope": "global" if is_global else addr_scope,
                }
            )
            if do_cleanup:
                if is_global:
                    global_addr_client.delete(
                        project=project, address=addr.name
                    ).result()
                else:
                    addr_client.delete(
                        project=project, region=addr_scope, address=addr.name
                    ).result()
                actions.append(
                    f"Deleted Compute Address: {addr.name} ({'global' if is_global else addr_scope})"
                )

    # 6. Networks & VPC Peerings (Global; peerings removed before network deletion)
    net_client = compute_v1.NetworksClient()
    for net in net_client.list(project=project):
        if match_prefix(net.name, prefix):
            found.append({"type": "compute.Network", "name": net.name})
            if do_cleanup:
                # Remove all peerings on this network before deleting it
                for peering in net.peerings or []:
                    try:
                        net_client.remove_peering(
                            project=project,
                            network=net.name,
                            networks_remove_peering_request_resource=compute_v1.NetworksRemovePeeringRequest(
                                name=peering.name
                            ),
                        ).result()
                        actions.append(
                            f"Removed VPC Peering: {peering.name} from {net.name}"
                        )
                    except Exception as e:
                        logging.warning(
                            f"Could not remove peering {peering.name} from {net.name}: {e}"
                        )
                net_client.delete(project=project, network=net.name).result()
                actions.append(f"Deleted Compute Network: {net.name}")

    return {"found": found, "actions": actions}


def handle_alloydb(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud import alloydb_v1

    client = alloydb_v1.AlloyDBAdminClient()
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    # 1. Backups
    try:
        for backup in client.list_backups(parent=parent):
            if match_prefix(backup.name, prefix):
                found.append({"type": "alloydb.Backup", "name": backup.name})
                if do_cleanup:
                    client.delete_backup(name=backup.name).result()
                    actions.append(f"Deleted AlloyDB Backup: {backup.name}")
    except Exception as e:
        logging.warning(f"AlloyDB backup list failed: {e}")

    # 2. Clusters, Instances & Users
    try:
        for cluster in client.list_clusters(parent=parent):
            cluster_matched = match_prefix(cluster.name, prefix) or match_prefix(
                cluster.display_name, prefix
            )

            # Sub-resources: Instances
            for instance in client.list_instances(parent=cluster.name):
                if (
                    cluster_matched
                    or match_prefix(instance.name, prefix)
                    or match_prefix(instance.display_name, prefix)
                ):
                    found.append({"type": "alloydb.Instance", "name": instance.name})
                    if do_cleanup:
                        client.delete_instance(name=instance.name).result()
                        actions.append(f"Deleted AlloyDB Instance: {instance.name}")

            # Sub-resources: Users
            for user in client.list_users(parent=cluster.name):
                if cluster_matched or match_prefix(user.name, prefix):
                    found.append({"type": "alloydb.User", "name": user.name})
                    if do_cleanup:
                        client.delete_user(name=user.name).result()
                        actions.append(f"Deleted AlloyDB User: {user.name}")

            # Parent Cluster
            if cluster_matched:
                found.append({"type": "alloydb.Cluster", "name": cluster.name})
                if do_cleanup:
                    client.delete_cluster(name=cluster.name).result()
                    actions.append(f"Deleted AlloyDB Cluster: {cluster.name}")
    except Exception as e:
        logging.warning(f"AlloyDB cluster list failed: {e}")

    return {"found": found, "actions": actions}


def handle_cloudbuild(project: str, prefix: str, do_cleanup: bool) -> HandlerResult:
    from google.cloud.devtools import cloudbuild_v1

    client = cloudbuild_v1.CloudBuildClient()
    found, actions = [], []

    try:
        for trigger in client.list_build_triggers(project_id=project):
            if match_prefix(trigger.name, prefix):
                found.append(
                    {
                        "type": "cloudbuild.Trigger",
                        "name": trigger.name,
                        "id": trigger.id,
                    }
                )
                if do_cleanup:
                    client.delete_build_trigger(
                        project_id=project, trigger_id=trigger.id
                    )
                    actions.append(f"Deleted CloudBuild Trigger: {trigger.name}")
    except Exception as e:
        logging.warning(f"CloudBuild list failed: {e}")

    return {"found": found, "actions": actions}


def handle_cloudbuildv2(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud.devtools import cloudbuild_v2

    client = cloudbuild_v2.RepositoryManagerClient()
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    try:
        for conn in client.list_connections(parent=parent):
            conn_matched = match_prefix(conn.name, prefix)

            # Repositories inside connection
            for repo in client.list_repositories(parent=conn.name):
                if conn_matched or match_prefix(repo.name, prefix):
                    found.append({"type": "cloudbuildv2.Repository", "name": repo.name})
                    if do_cleanup:
                        client.delete_repository(name=repo.name).result()
                        actions.append(f"Deleted CloudBuild v2 Repository: {repo.name}")

            if conn_matched:
                found.append({"type": "cloudbuildv2.Connection", "name": conn.name})
                if do_cleanup:
                    client.delete_connection(name=conn.name).result()
                    actions.append(f"Deleted CloudBuild v2 Connection: {conn.name}")
    except Exception as e:
        logging.warning(f"CloudBuild v2 list failed: {e}")

    return {"found": found, "actions": actions}


def handle_colab(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud import aiplatform_v1

    opts = {"api_endpoint": f"{region}-aiplatform.googleapis.com"}
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    # 1. Schedules
    sched_client = aiplatform_v1.ScheduleServiceClient(client_options=opts)
    for sched in sched_client.list_schedules(parent=parent):
        if match_prefix(sched.name, prefix) or match_prefix(sched.display_name, prefix):
            found.append({"type": "colab.Schedule", "name": sched.name})
            if do_cleanup:
                sched_client.delete_schedule(name=sched.name).result()
                actions.append(f"Deleted Colab Schedule: {sched.name}")

    # 2. Notebook Runtimes, Execution Jobs, Runtime Templates
    nb_client = aiplatform_v1.NotebookServiceClient(client_options=opts)

    for job in nb_client.list_notebook_execution_jobs(parent=parent):
        if match_prefix(job.name, prefix) or match_prefix(job.display_name, prefix):
            found.append({"type": "colab.NotebookExecution", "name": job.name})
            if do_cleanup:
                nb_client.delete_notebook_execution_job(name=job.name).result()
                actions.append(f"Deleted Colab Notebook Execution Job: {job.name}")

    for runtime in nb_client.list_notebook_runtimes(parent=parent):
        if match_prefix(runtime.name, prefix) or match_prefix(
            runtime.display_name, prefix
        ):
            found.append({"type": "colab.Runtime", "name": runtime.name})
            if do_cleanup:
                nb_client.delete_notebook_runtime(name=runtime.name).result()
                actions.append(f"Deleted Colab Runtime: {runtime.name}")

    for tmpl in nb_client.list_notebook_runtime_templates(parent=parent):
        if match_prefix(tmpl.name, prefix) or match_prefix(tmpl.display_name, prefix):
            found.append({"type": "colab.RuntimeTemplate", "name": tmpl.name})
            if do_cleanup:
                nb_client.delete_notebook_runtime_template(name=tmpl.name).result()
                actions.append(f"Deleted Colab Runtime Template: {tmpl.name}")

    return {"found": found, "actions": actions}


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
    vertex_prefix = prefix.replace("-", "_")

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
    ) -> None:
        try:
            client = client_cls(client_options=opts)
            for item in getattr(client, list_method)(parent=parent):
                name = getattr(item, "name", "")
                disp = getattr(item, "display_name", "")
                if vertex_match(name, disp):
                    found.append({"type": f"vertexai.{asset_type}", "name": name})
                    if do_cleanup:
                        getattr(client, delete_method)(name=name).result()
                        actions.append(f"Deleted Vertex AI {asset_type}: {name}")
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
            for item in getattr(client, list_method)(parent=parent):
                name = getattr(item, "name", "")
                disp = getattr(item, "display_name", "")
                if not vertex_match(name, disp):
                    continue
                found.append({"type": f"vertexai.{asset_type}", "name": name})
                if do_cleanup:
                    if getattr(item, "state", None) not in _TERMINAL_JOB_STATES:
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
        except Exception as e:
            msg = f"Vertex AI {asset_type} scan failed: {e}"
            logging.warning(msg)
            actions.append(f"[WARN] {msg}")

    # Endpoints (Undeploys models before deletion)
    try:
        ep_client = aiplatform_v1.EndpointServiceClient(client_options=opts)
        for ep in ep_client.list_endpoints(parent=parent):
            if vertex_match(ep.name, ep.display_name):
                found.append({"type": "vertexai.Endpoint", "name": ep.name})
                if do_cleanup:
                    if ep.deployed_models:
                        for dm in ep.deployed_models:
                            ep_client.undeploy_endpoint(
                                request=aiplatform_v1.UndeployEndpointRequest(
                                    endpoint=ep.name, deployed_model_id=dm.id
                                )
                            ).result()
                    ep_client.delete_endpoint(name=ep.name).result()
                    actions.append(f"Deleted Vertex AI Endpoint: {ep.name}")
    except Exception as e:
        msg = f"Vertex AI Endpoint scan failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    # Models (must come after Endpoints so deployed-model references are already removed)
    scan_and_delete(
        aiplatform_v1.ModelServiceClient, "list_models", "delete_model", "Model"
    )

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
        for ie in ie_client.list_index_endpoints(parent=parent):
            if vertex_match(ie.name, ie.display_name):
                found.append({"type": "vertexai.IndexEndpoint", "name": ie.name})
                if do_cleanup:
                    for di in ie.deployed_indexes or []:
                        ie_client.undeploy_index(
                            request=aiplatform_v1.UndeployIndexRequest(
                                index_endpoint=ie.name, deployed_index_id=di.id
                            )
                        ).result()
                    ie_client.delete_index_endpoint(name=ie.name).result()
                    actions.append(f"Deleted Vertex AI IndexEndpoint: {ie.name}")
    except Exception as e:
        msg = f"Vertex AI IndexEndpoint scan failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    scan_and_delete(
        aiplatform_v1.IndexServiceClient, "list_indexes", "delete_index", "Index"
    )
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

    # Featurestore & FeatureOnlineStore
    scan_and_delete(
        aiplatform_v1.FeaturestoreServiceClient,
        "list_featurestores",
        "delete_featurestore",
        "Featurestore",
    )
    scan_and_delete(
        aiplatform_v1.FeatureOnlineStoreAdminServiceClient,
        "list_feature_online_stores",
        "delete_feature_online_store",
        "FeatureOnlineStore",
    )
    scan_and_delete(
        aiplatform_v1.FeatureRegistryServiceClient,
        "list_feature_groups",
        "delete_feature_group",
        "FeatureGroup",
    )

    return {"found": found, "actions": actions}


# --- HTTP Entrypoint ---


@functions_framework.http
def scan_and_clean_resources(
    request: Request,
) -> tuple[str, int] | tuple[str, int, dict[str, str]]:
    project_id = os.environ.get("TARGET_GCP_PROJECT_ID")
    region = request.args.get(
        "region", os.environ.get("REGION", DEFAULT_REGION)
    ).lower()
    prefix = request.args.get(
        "prefix", os.environ.get("PREFIX", DEFAULT_PREFIX)
    ).lower()
    do_cleanup = (
        request.args.get("do_cleanup", os.environ.get("DO_CLEANUP", "false")).lower()
        == "true"
    )

    if not project_id:
        return json.dumps({"error": "TARGET_GCP_PROJECT_ID not set."}), 400

    summary = {
        "mode": "ACTIVE DESTRUCTION" if do_cleanup else "DRY-RUN (Discovery Only)",
        "project": project_id,
        "region": region,
        "prefix_filter": prefix,
        "found_resources": [],
        "actions_taken": [],
    }

    # Execute modular scanners in dependency order:
    # VPC consumers (alloydb, cloudbuild, colab, vertexai) must be torn down
    # before compute networks, because PSA peerings and other VPC attachments
    # block network deletion.
    scanners = [
        ("alloydb", lambda: handle_alloydb(project_id, region, prefix, do_cleanup)),
        ("cloudbuild", lambda: handle_cloudbuild(project_id, prefix, do_cleanup)),
        (
            "cloudbuildv2",
            lambda: handle_cloudbuildv2(project_id, region, prefix, do_cleanup),
        ),
        ("colab", lambda: handle_colab(project_id, region, prefix, do_cleanup)),
        ("vertexai", lambda: handle_vertexai(project_id, region, prefix, do_cleanup)),
        ("compute", lambda: handle_compute(project_id, region, prefix, do_cleanup)),
    ]

    for domain, runner in scanners:
        try:
            res = runner()
            summary["found_resources"].extend(res["found"])
            summary["actions_taken"].extend(res["actions"])
        except Exception as e:
            logging.error(f"Error during {domain} scanner execution: {e}")
            summary["actions_taken"].append(f"Error scanning {domain}: {str(e)}")

    summary["found_count"] = len(summary["found_resources"])

    return json.dumps(summary, indent=2), 200, {"Content-Type": "application/json"}
