import json
import os
import re

import functions_framework
from google.cloud import asset_v1  # Discovery client only

# Execution order satisfying GCP dependency trees
DELETION_ORDER = [
    # 1. Scheduled Triggers & Active Executions (Must cancel before removing runtimes)
    "aiplatform.googleapis.com/Schedule",
    "aiplatform.googleapis.com/NotebookExecutionJob",
    # 2. Active Runtimes & Serving Endpoints
    "aiplatform.googleapis.com/Endpoint",
    "aiplatform.googleapis.com/NotebookRuntime",
    # 3. Configuration Templates (Must delete runtimes referencing these first)
    "aiplatform.googleapis.com/NotebookRuntimeTemplate",
    # 4. Database Instances
    "alloydb.googleapis.com/Instance",
    # 5. Intermediate Parent Resources & Artifacts
    "alloydb.googleapis.com/Cluster",
    "aiplatform.googleapis.com/Model",
    "aiplatform.googleapis.com/PipelineJob",
    "aiplatform.googleapis.com/Dataset",
    # 6. Low-Level Networking Infrastructure
    "compute.googleapis.com/Address",
    "compute.googleapis.com/Subnetwork",
    "compute.googleapis.com/Network",
]


def parse_gcp_path(full_name: str):
    """Parses a CAI asset URI into clean parts for API consumption."""
    clean_path = re.sub(r"^//[^/]+/", "", full_name)
    project_match = re.search(r"projects/([^/]+)", clean_path)
    location_match = re.search(r"(locations|regions)/([^/]+)", clean_path)

    return {
        "project": project_match.group(1) if project_match else None,
        "location": location_match.group(2) if location_match else None,
        "resource_id": clean_path.split("/")[-1],
        "clean_path": clean_path,
    }


def cleanup_orphaned_peerings(project_id: str) -> list:
    """Finds and drops VPC network peerings starting with 'ansible-test-'"""
    from google.cloud import compute_v1  # Lazy import

    networks_client = compute_v1.NetworksClient()
    actions_taken = []
    try:
        networks = networks_client.list(project=project_id)
        for network in networks:
            if network.peerings:
                for peering in network.peerings:
                    if peering.name.startswith("ansible-test"):
                        print(
                            f"Dropping peering '{peering.name}' on network '{network.name}'..."
                        )
                        req = compute_v1.RemovePeeringNetworkRequest(
                            project=project_id,
                            network=network.name,
                            networks_remove_peering_request_resource=compute_v1.NetworksRemovePeeringRequest(
                                name=peering.name
                            ),
                        )
                        networks_client.remove_peering(request=req).result()
                        actions_taken.append(
                            f"Removed peering '{peering.name}' from network '{network.name}'"
                        )
    except Exception as e:
        actions_taken.append(f"Failed during peering scan: {str(e)}")
    return actions_taken


# --- Modular Deletion Handlers ---


def delete_alloydb_resource(asset_type: str, clean_path: str) -> str:
    from google.cloud import alloydb_v1  # Lazy import

    client = alloydb_v1.AlloyDBAdminClient()

    if asset_type == "alloydb.googleapis.com/Instance":
        client.delete_instance(name=clean_path).result()
        return f"Deleted AlloyDB Instance: {clean_path}"
    elif asset_type == "alloydb.googleapis.com/Cluster":
        client.delete_cluster(name=clean_path).result()
        return f"Deleted AlloyDB Cluster: {clean_path}"
    return f"Unhandled AlloyDB asset type: {asset_type}"


def get_vertex_client_options(clean_path: str) -> dict:
    """Extracts location from resource path and returns regional client_options."""
    parts = clean_path.split("/")
    if "locations" in parts:
        loc_idx = parts.index("locations") + 1
        if loc_idx < len(parts):
            location = parts[loc_idx]
            return {"api_endpoint": f"{location}-aiplatform.googleapis.com"}
    return {"api_endpoint": "us-central1-aiplatform.googleapis.com"}


def delete_vertex_resource(asset_type: str, clean_path: str) -> str:
    from google.cloud import aiplatform_v1  # Lazy import single SDK

    # Get regional endpoint options dynamically from resource path
    opts = get_vertex_client_options(clean_path)

    if asset_type == "aiplatform.googleapis.com/Schedule":
        client = aiplatform_v1.ScheduleServiceClient(client_options=opts)
        client.delete_schedule(name=clean_path).result()
        return f"Deleted Vertex Schedule: {clean_path}"

    elif asset_type in (
        "aiplatform.googleapis.com/NotebookExecutionJob",
        "aiplatform.googleapis.com/NotebookRuntime",
        "aiplatform.googleapis.com/NotebookRuntimeTemplate",
    ):
        client = aiplatform_v1.NotebookServiceClient(client_options=opts)
        if asset_type == "aiplatform.googleapis.com/NotebookExecutionJob":
            client.delete_notebook_execution_job(name=clean_path).result()
            return f"Deleted Vertex Notebook Execution Job: {clean_path}"
        elif asset_type == "aiplatform.googleapis.com/NotebookRuntime":
            client.delete_notebook_runtime(name=clean_path).result()
            return f"Deleted Vertex Notebook Runtime: {clean_path}"
        elif asset_type == "aiplatform.googleapis.com/NotebookRuntimeTemplate":
            client.delete_notebook_runtime_template(name=clean_path).result()
            return f"Deleted Vertex Notebook Runtime Template: {clean_path}"

    elif asset_type == "aiplatform.googleapis.com/Endpoint":
        client = aiplatform_v1.EndpointServiceClient(client_options=opts)
        endpoint_obj = client.get_endpoint(name=clean_path)
        if endpoint_obj.deployed_models:
            for dm in endpoint_obj.deployed_models:
                client.undeploy_endpoint(
                    request=aiplatform_v1.UndeployEndpointRequest(
                        endpoint=clean_path, deployed_model_id=dm.id
                    )
                ).result()
        client.delete_endpoint(name=clean_path).result()
        return f"Deleted Vertex Endpoint: {clean_path}"

    elif asset_type == "aiplatform.googleapis.com/Model":
        client = aiplatform_v1.ModelServiceClient(client_options=opts)
        client.delete_model(name=clean_path).result()
        return f"Deleted Vertex Model: {clean_path}"

    elif asset_type == "aiplatform.googleapis.com/PipelineJob":
        client = aiplatform_v1.PipelineServiceClient(client_options=opts)
        client.delete_pipeline_job(name=clean_path).result()
        return f"Deleted Vertex Pipeline Job: {clean_path}"

    elif asset_type == "aiplatform.googleapis.com/Dataset":
        client = aiplatform_v1.DatasetServiceClient(client_options=opts)
        client.delete_dataset(name=clean_path).result()
        return f"Deleted Vertex Dataset: {clean_path}"

    return f"Unhandled Vertex asset type: {asset_type}"


def delete_compute_resource(
    asset_type: str, clean_path: str, project: str, location: str, resource_id: str
) -> str:
    from google.cloud import compute_v1  # Lazy import

    if asset_type == "compute.googleapis.com/Address":
        if "global" in clean_path:
            client = compute_v1.GlobalAddressesClient()
            client.delete(project=project, address=resource_id).result()
        else:
            client = compute_v1.AddressesClient()
            client.delete(
                project=project, region=location, address=resource_id
            ).result()
        return f"Deleted Compute IP Address: {clean_path}"

    elif asset_type == "compute.googleapis.com/Subnetwork":
        client = compute_v1.SubnetworksClient()
        client.delete(project=project, region=location, subnetwork=resource_id).result()
        return f"Deleted VPC Subnetwork: {clean_path}"

    elif asset_type == "compute.googleapis.com/Network":
        client = compute_v1.NetworksClient()
        client.delete(project=project, network=resource_id).result()
        return f"Deleted VPC Network: {clean_path}"

    return f"Unhandled Compute asset type: {asset_type}"


def delete_service_networking_connection(clean_path: str) -> str:
    """
    Deletes a Service Networking Connection using the official Google API Client Library.
    clean_path format from CAI: projects/{PROJECT_ID}/global/networks/{NETWORK_NAME}
    """
    from googleapiclient import discovery  # Lazy import

    consumer_network = (
        clean_path[clean_path.find("projects/") :]
        if "projects/" in clean_path
        else clean_path
    )

    # Instantiate the official Service Networking v1 client
    service = discovery.build("servicenetworking", "v1", cache_discovery=False)

    connection_name = "services/servicenetworking.googleapis.com/connections/servicenetworking-googleapis-com"
    request_body = {"consumerNetwork": consumer_network}

    operation = (
        service.services()
        .connections()
        .deleteConnection(name=connection_name, body=request_body)
        .execute()
    )

    return f"Initiated Service Networking Connection deletion for {consumer_network} (Operation: {operation.get('name')})"


# --- Execution Router ---
def execute_deletion(asset_type: str, full_name: str) -> str:
    parsed = parse_gcp_path(full_name)
    clean_path = parsed["clean_path"]
    project = parsed["project"]
    location = parsed["location"]
    resource_id = parsed["resource_id"]

    try:
        if asset_type.startswith("alloydb.googleapis.com"):
            return delete_alloydb_resource(asset_type, clean_path)

        elif asset_type.startswith("aiplatform.googleapis.com"):
            return delete_vertex_resource(asset_type, clean_path)

        elif asset_type.startswith("compute.googleapis.com"):
            return delete_compute_resource(
                asset_type, clean_path, project, location, resource_id
            )

        elif asset_type == "servicenetworking.googleapis.com/Connection":
            return delete_service_networking_connection(clean_path)

        return f"Unhandled asset type: {asset_type}"

    except Exception as e:
        return f"Error deleting {clean_path}: {str(e)}"


@functions_framework.http
def scan_and_clean_resources(request):
    project_id = os.environ.get("TARGET_GCP_PROJECT_ID")
    do_cleanup = os.environ.get("DO_CLEANUP", "false").lower() == "true"

    if not project_id:
        return json.dumps({"error": "TARGET_GCP_PROJECT_ID not configured."}), 400

    try:
        client = asset_v1.AssetServiceClient()
        scope = f"projects/{project_id}"
        query = "(name:ansible-test* OR displayName:ansible-test*)"

        response = client.search_all_resources(request={"scope": scope, "query": query})

        found_assets = []
        for resource in response:
            found_assets.append(
                {
                    "asset_type": resource.asset_type,
                    "name": resource.name,
                    "created_at": str(resource.create_time),
                }
            )

        log_summary = {
            "mode": "ACTIVE DESTRUCTION" if do_cleanup else "DRY-RUN (Discovery Only)",
            "found_count": len(found_assets),
            "found_resources": found_assets,
            "actions_taken": [],
        }

        if do_cleanup and found_assets:
            peering_actions = cleanup_orphaned_peerings(project_id)
            log_summary["actions_taken"].extend(peering_actions)

            found_assets.sort(
                key=lambda x: (
                    DELETION_ORDER.index(x["asset_type"])
                    if x["asset_type"] in DELETION_ORDER
                    else len(DELETION_ORDER)
                )
            )

            for asset in found_assets:
                result_msg = execute_deletion(asset["asset_type"], asset["name"])
                log_summary["actions_taken"].append(result_msg)

        return (
            json.dumps(log_summary, indent=2),
            200,
            {"Content-Type": "application/json"},
        )

    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}
