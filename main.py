from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Callable, TypedDict

import functions_framework

if TYPE_CHECKING:
    from flask import Request


class HandlerResult(TypedDict):
    found: list[dict[str, str]]
    actions: list[str]


# Standard configuration defaults
DEFAULT_REGION = "us-central1"
DEFAULT_PREFIX = "ansible-test-"

# Bounded worker count for concurrent delete operations. Deletes are
# I/O-bound (almost all wall time is spent blocked on GCP long-running
# operations), so this is intentionally NOT derived from CPU core count —
# doing so would collapse concurrency back to ~1 on a single-vCPU Cloud Run
# instance. The value is a fixed middle ground: comfortably more than a
# typical leftover-resource batch, well under GCP's per-project concurrent
# operation limits.
MAX_DELETE_WORKERS = 8


def match_prefix(name: str, prefix: str) -> bool:
    """Checks if a resource name or display_name matches the filter prefix."""
    if not name:
        return False
    # Handle resource paths by taking the short resource ID
    short_name = name.split("/")[-1]
    return short_name.startswith(prefix)


def underscore_prefix(prefix: str) -> str:
    """Returns the prefix with dashes converted to underscores.

    Some GCP resources (Vertex AI Featurestore/FeatureOnlineStore/FeatureGroup,
    BigQuery datasets/tables) disallow dashes in their IDs. Ansible converts the
    configured prefix to underscores when naming those resources, so callers
    match both the original and underscore form.
    """
    return prefix.replace("-", "_")


def _run_concurrent(tasks: list[Callable[[], None]]) -> None:
    """Runs zero-argument callables concurrently, bounded by
    MAX_DELETE_WORKERS, blocking until all have completed.

    This exists because every delete call in this module blocks on a GCP
    long-running operation (`.result()`), and running them serially causes
    the cumulative wait time to risk exceeding the Cloud Function's timeout.
    Each callable is expected to handle its own errors and append its own
    success/failure messages to the shared `actions` list where
    appropriate — CPython's list/set append/add are atomic across threads,
    so no additional locking is required. Callables that don't catch their
    own exceptions will have them re-raised here once all tasks complete.
    """
    if not tasks:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DELETE_WORKERS) as pool:
        futures = [pool.submit(task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def handle_compute(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    import google.auth
    import google_auth_httplib2
    import httplib2
    from google.cloud import compute_v1
    from googleapiclient.discovery import build

    found, actions = [], []

    # 1. Instances
    inst_client = compute_v1.InstancesClient()
    instance_tasks: list[Callable[[], None]] = []
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

                            def _delete_instance(inst=inst, zone=zone) -> None:
                                inst_client.delete(
                                    project=project, zone=zone, instance=inst.name
                                ).result()
                                actions.append(
                                    f"Deleted Compute Instance: {inst.name} ({zone})"
                                )

                            instance_tasks.append(_delete_instance)
    _run_concurrent(instance_tasks)

    # 2. Disks
    disk_client = compute_v1.DisksClient()
    disk_tasks: list[Callable[[], None]] = []
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

                            def _delete_disk(disk=disk, zone=zone) -> None:
                                disk_client.delete(
                                    project=project, zone=zone, disk=disk.name
                                ).result()
                                actions.append(
                                    f"Deleted Compute Disk: {disk.name} ({zone})"
                                )

                            disk_tasks.append(_delete_disk)
    _run_concurrent(disk_tasks)

    # 3. Firewall Rules (must go before networks)
    fw_client = compute_v1.FirewallsClient()
    firewall_tasks: list[Callable[[], None]] = []
    for fw in fw_client.list(project=project):
        if match_prefix(fw.name, prefix):
            found.append({"type": "compute.Firewall", "name": fw.name})
            if do_cleanup:

                def _delete_firewall(fw=fw) -> None:
                    fw_client.delete(project=project, firewall=fw.name).result()
                    actions.append(f"Deleted Compute Firewall: {fw.name}")

                firewall_tasks.append(_delete_firewall)
    _run_concurrent(firewall_tasks)

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
    subnet_tasks: list[Callable[[], None]] = []
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

                            def _delete_subnet(
                                subnet=subnet, subnet_region=subnet_region
                            ) -> None:
                                subnet_client.delete(
                                    project=project,
                                    region=subnet_region,
                                    subnetwork=subnet.name,
                                ).result()
                                actions.append(
                                    f"Deleted Compute Subnetwork: {subnet.name}"
                                )

                            subnet_tasks.append(_delete_subnet)
    _run_concurrent(subnet_tasks)

    # 5. Service Networking Connections (must go before addresses & networks —
    # the allocated PEERING ranges backing these connections, e.g. those
    # created via `gcloud services vpc-peerings connect`, cannot be deleted
    # while the connection is still active, and the connection itself is a
    # peering that blocks network deletion).
    matched_networks = [
        net.name
        for net in _net_pre.list(project=project)
        if match_prefix(net.name, prefix)
    ]
    # Networks whose Service Networking connection cleanup failed — deleting
    # the network anyway would orphan the producer-side connection and any
    # managed services (e.g. AlloyDB) still using it.
    networks_with_failed_connections: set[str] = set()
    if matched_networks:
        # The Service Networking API requires the consumer network to be
        # addressed by numeric project number, not project ID — passing the
        # project ID causes a misleading 404 "Producer tenant project not
        # found" error. Note: compute_v1's Project.id is NOT the project
        # number (it's an unrelated internal Compute resource identifier) —
        # the Cloud Resource Manager API is the only reliable source for it.
        from google.cloud import resourcemanager_v3

        rm_client = resourcemanager_v3.ProjectsClient()
        project_number = rm_client.get_project(name=f"projects/{project}").name.split(
            "/"
        )[-1]
        # google-api-python-client is httplib2-based, and httplib2.Http()
        # objects are explicitly documented as not thread-safe — each thread
        # must use its own instance. The `service_networking` object itself
        # is safe to share (only used to build request objects), but every
        # concurrent `.execute()` call below is given its own transport.
        credentials, _ = google.auth.default()
        service_networking = build(
            "servicenetworking", "v1", credentials=credentials, cache_discovery=False
        )
        connection_tasks: list[Callable[[], None]] = []
        for net_name in matched_networks:
            consumer_network = f"projects/{project_number}/global/networks/{net_name}"
            try:
                response = (
                    service_networking.services()
                    .connections()
                    .list(
                        parent="services/servicenetworking.googleapis.com",
                        network=consumer_network,
                    )
                    .execute()
                )
            except Exception as e:
                msg = (
                    f"Could not list Service Networking connections for {net_name}: {e}"
                )
                logging.warning(msg)
                actions.append(f"[WARN] {msg}")
                # Unable to verify connection state — treat conservatively as
                # unsafe, same as a failed delete, so the network isn't
                # deleted out from under an unconfirmed connection.
                networks_with_failed_connections.add(net_name)
                continue
            for conn in response.get("connections", []):
                peering_name = conn.get("peering", "")
                found.append(
                    {
                        "type": "servicenetworking.Connection",
                        "name": peering_name,
                        "network": net_name,
                    }
                )
                if do_cleanup:

                    def _delete_connection(
                        peering_name=peering_name,
                        net_name=net_name,
                        consumer_network=consumer_network,
                    ) -> None:
                        # Thread-local transport — see thread-safety note above.
                        thread_http = google_auth_httplib2.AuthorizedHttp(
                            credentials, http=httplib2.Http()
                        )
                        try:
                            op = (
                                service_networking.services()
                                .connections()
                                .deleteConnection(
                                    name=f"services/servicenetworking.googleapis.com/connections/{peering_name}",
                                    body={"consumerNetwork": consumer_network},
                                )
                                .execute(http=thread_http)
                            )
                            while not op.get("done"):
                                time.sleep(2)
                                op = (
                                    service_networking.operations()
                                    .get(name=op["name"])
                                    .execute(http=thread_http)
                                )
                            if "error" in op:
                                logging.warning(
                                    f"Service Networking connection deletion failed for {peering_name} ({net_name}): {op['error']}"
                                )
                                networks_with_failed_connections.add(net_name)
                            else:
                                actions.append(
                                    f"Deleted Service Networking Connection: {peering_name} ({net_name})"
                                )
                        except Exception as e:
                            logging.warning(
                                f"Could not delete Service Networking connection {peering_name} for {net_name}: {e}"
                            )
                            networks_with_failed_connections.add(net_name)

                    connection_tasks.append(_delete_connection)
        _run_concurrent(connection_tasks)

    # 6. Static/External IP Addresses (must go before networks)
    # aggregated_list returns regional addresses under "regions/{region}" keys
    # and global addresses under the "global" key — each requires a different
    # client and delete call.
    global_addr_client = compute_v1.GlobalAddressesClient()
    addr_client = compute_v1.AddressesClient()
    address_tasks: list[Callable[[], None]] = []
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

                def _delete_address(
                    addr=addr, addr_scope=addr_scope, is_global=is_global
                ) -> None:
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

                address_tasks.append(_delete_address)
    _run_concurrent(address_tasks)

    # 7. Networks & VPC Peerings (Global; peerings removed before network deletion)
    net_client = compute_v1.NetworksClient()
    network_tasks: list[Callable[[], None]] = []
    for net in net_client.list(project=project):
        if match_prefix(net.name, prefix):
            found.append({"type": "compute.Network", "name": net.name})
            if do_cleanup:
                if net.name in networks_with_failed_connections:
                    msg = (
                        f"Skipping deletion of Compute Network {net.name}: "
                        "Service Networking connection cleanup failed earlier"
                    )
                    logging.warning(msg)
                    actions.append(f"[WARN] {msg}")
                    continue

                def _delete_network(net=net) -> None:
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

                network_tasks.append(_delete_network)
    _run_concurrent(network_tasks)

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
        backup_tasks: list[Callable[[], None]] = []
        for backup in client.list_backups(parent=parent):
            if match_prefix(backup.name, prefix):
                found.append({"type": "alloydb.Backup", "name": backup.name})
                if do_cleanup:

                    def _delete_backup(backup=backup) -> None:
                        client.delete_backup(name=backup.name).result()
                        actions.append(f"Deleted AlloyDB Backup: {backup.name}")

                    backup_tasks.append(_delete_backup)
        _run_concurrent(backup_tasks)
    except Exception as e:
        msg = f"AlloyDB backup list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    # 2. Clusters, Instances & Users
    try:
        sub_resource_tasks: list[Callable[[], None]] = []
        matched_clusters = []
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

                        def _delete_instance(instance=instance) -> None:
                            client.delete_instance(name=instance.name).result()
                            actions.append(f"Deleted AlloyDB Instance: {instance.name}")

                        sub_resource_tasks.append(_delete_instance)

            # Sub-resources: Users
            for user in client.list_users(parent=cluster.name):
                if cluster_matched or match_prefix(user.name, prefix):
                    found.append({"type": "alloydb.User", "name": user.name})
                    if do_cleanup:

                        def _delete_user(user=user) -> None:
                            client.delete_user(name=user.name).result()
                            actions.append(f"Deleted AlloyDB User: {user.name}")

                        sub_resource_tasks.append(_delete_user)

            # Parent Cluster
            if cluster_matched:
                found.append({"type": "alloydb.Cluster", "name": cluster.name})
                if do_cleanup:
                    matched_clusters.append(cluster)

        # Instances & Users must be fully deleted before their parent
        # cluster; clusters themselves are independent of each other.
        _run_concurrent(sub_resource_tasks)

        cluster_tasks: list[Callable[[], None]] = []
        for cluster in matched_clusters:

            def _delete_cluster(cluster=cluster) -> None:
                client.delete_cluster(name=cluster.name).result()
                actions.append(f"Deleted AlloyDB Cluster: {cluster.name}")

            cluster_tasks.append(_delete_cluster)
        _run_concurrent(cluster_tasks)
    except Exception as e:
        msg = f"AlloyDB cluster list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    return {"found": found, "actions": actions}


def handle_cloudbuild(project: str, prefix: str, do_cleanup: bool) -> HandlerResult:
    from google.cloud.devtools import cloudbuild_v1

    client = cloudbuild_v1.CloudBuildClient()
    found, actions = [], []

    try:
        trigger_tasks: list[Callable[[], None]] = []
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

                    def _delete_trigger(trigger=trigger) -> None:
                        client.delete_build_trigger(
                            project_id=project, trigger_id=trigger.id
                        )
                        actions.append(f"Deleted CloudBuild Trigger: {trigger.name}")

                    trigger_tasks.append(_delete_trigger)
        _run_concurrent(trigger_tasks)
    except Exception as e:
        msg = f"CloudBuild list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    return {"found": found, "actions": actions}


def handle_cloudbuildv2(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud.devtools import cloudbuild_v2

    client = cloudbuild_v2.RepositoryManagerClient()
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    try:
        repo_tasks: list[Callable[[], None]] = []
        matched_connections = []
        for conn in client.list_connections(parent=parent):
            conn_matched = match_prefix(conn.name, prefix)

            # Repositories inside connection
            for repo in client.list_repositories(parent=conn.name):
                if conn_matched or match_prefix(repo.name, prefix):
                    found.append({"type": "cloudbuildv2.Repository", "name": repo.name})
                    if do_cleanup:

                        def _delete_repo(repo=repo) -> None:
                            client.delete_repository(name=repo.name).result()
                            actions.append(
                                f"Deleted CloudBuild v2 Repository: {repo.name}"
                            )

                        repo_tasks.append(_delete_repo)

            if conn_matched:
                found.append({"type": "cloudbuildv2.Connection", "name": conn.name})
                if do_cleanup:
                    matched_connections.append(conn)

        # Repositories must be deleted before their parent connection.
        _run_concurrent(repo_tasks)

        connection_tasks: list[Callable[[], None]] = []
        for conn in matched_connections:

            def _delete_connection(conn=conn) -> None:
                client.delete_connection(name=conn.name).result()
                actions.append(f"Deleted CloudBuild v2 Connection: {conn.name}")

            connection_tasks.append(_delete_connection)
        _run_concurrent(connection_tasks)
    except Exception as e:
        msg = f"CloudBuild v2 list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

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
    schedule_tasks: list[Callable[[], None]] = []
    for sched in sched_client.list_schedules(parent=parent):
        if match_prefix(sched.name, prefix) or match_prefix(sched.display_name, prefix):
            found.append({"type": "colab.Schedule", "name": sched.name})
            if do_cleanup:

                def _delete_schedule(sched=sched) -> None:
                    sched_client.delete_schedule(name=sched.name).result()
                    actions.append(f"Deleted Colab Schedule: {sched.name}")

                schedule_tasks.append(_delete_schedule)
    _run_concurrent(schedule_tasks)

    # 2. Notebook Runtimes, Execution Jobs, Runtime Templates
    nb_client = aiplatform_v1.NotebookServiceClient(client_options=opts)

    job_tasks: list[Callable[[], None]] = []
    for job in nb_client.list_notebook_execution_jobs(parent=parent):
        if match_prefix(job.name, prefix) or match_prefix(job.display_name, prefix):
            found.append({"type": "colab.NotebookExecution", "name": job.name})
            if do_cleanup:

                def _delete_job(job=job) -> None:
                    nb_client.delete_notebook_execution_job(name=job.name).result()
                    actions.append(f"Deleted Colab Notebook Execution Job: {job.name}")

                job_tasks.append(_delete_job)
    _run_concurrent(job_tasks)

    runtime_tasks: list[Callable[[], None]] = []
    for runtime in nb_client.list_notebook_runtimes(parent=parent):
        if match_prefix(runtime.name, prefix) or match_prefix(
            runtime.display_name, prefix
        ):
            found.append({"type": "colab.Runtime", "name": runtime.name})
            if do_cleanup:

                def _delete_runtime(runtime=runtime) -> None:
                    nb_client.delete_notebook_runtime(name=runtime.name).result()
                    actions.append(f"Deleted Colab Runtime: {runtime.name}")

                runtime_tasks.append(_delete_runtime)
    _run_concurrent(runtime_tasks)

    template_tasks: list[Callable[[], None]] = []
    for tmpl in nb_client.list_notebook_runtime_templates(parent=parent):
        if match_prefix(tmpl.name, prefix) or match_prefix(tmpl.display_name, prefix):
            found.append({"type": "colab.RuntimeTemplate", "name": tmpl.name})
            if do_cleanup:

                def _delete_template(tmpl=tmpl) -> None:
                    nb_client.delete_notebook_runtime_template(name=tmpl.name).result()
                    actions.append(f"Deleted Colab Runtime Template: {tmpl.name}")

                template_tasks.append(_delete_template)
    _run_concurrent(template_tasks)

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
            tasks: list[Callable[[], None]] = []
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
            tasks: list[Callable[[], None]] = []
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
            undeploy_tasks: list[Callable[[], None]] = []
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
            model_tasks: list[Callable[[], None]] = []
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
        endpoint_tasks: list[Callable[[], None]] = []
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
        index_endpoint_tasks: list[Callable[[], None]] = []
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
            artifact_tasks: list[Callable[[], None]] = []
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
            context_tasks: list[Callable[[], None]] = []
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
            execution_tasks: list[Callable[[], None]] = []
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


def handle_storage(project: str, prefix: str, do_cleanup: bool) -> HandlerResult:
    from google.cloud import storage

    client = storage.Client(project=project)
    found: list[dict[str, str]] = []
    actions: list[str] = []

    try:
        bucket_tasks: list[Callable[[], None]] = []
        for bucket in client.list_buckets():
            if not match_prefix(bucket.name, prefix):
                continue
            found.append({"type": "storage.Bucket", "name": bucket.name})
            if do_cleanup:

                def _delete_bucket(bucket_name: str = bucket.name) -> None:
                    try:
                        b = client.bucket(bucket_name)
                        b.delete(force=True)
                        actions.append(f"Deleted Storage Bucket: {bucket_name}")
                    except Exception as e:
                        msg = f"Could not delete Storage Bucket {bucket_name}: {e}"
                        logging.warning(msg)
                        actions.append(f"[WARN] {msg}")

                bucket_tasks.append(_delete_bucket)
        _run_concurrent(bucket_tasks)
    except Exception as e:
        msg = f"Storage bucket list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    return {"found": found, "actions": actions}


def handle_bigquery(project: str, prefix: str, do_cleanup: bool) -> HandlerResult:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    found: list[dict[str, str]] = []
    actions: list[str] = []

    # BigQuery dataset IDs allow only letters, numbers, and underscores (no dashes).
    # Ansible converts the prefix to underscores when naming datasets, so we match both.
    bq_prefix = underscore_prefix(prefix)

    def bq_match(name: str) -> bool:
        return match_prefix(name, prefix) or match_prefix(name, bq_prefix)

    try:
        dataset_tasks: list[Callable[[], None]] = []
        for dataset in client.list_datasets():
            if not bq_match(dataset.dataset_id):
                continue
            found.append({"type": "bigquery.Dataset", "name": dataset.dataset_id})
            if do_cleanup:

                def _delete_dataset(dataset_id: str = dataset.dataset_id) -> None:
                    try:
                        # delete_contents=True cascades to all contained tables, views,
                        # routines, and BQML models in one call.
                        client.delete_dataset(
                            dataset_id, delete_contents=True, not_found_ok=True
                        )
                        actions.append(f"Deleted BigQuery Dataset: {dataset_id}")
                    except Exception as e:
                        msg = f"Could not delete BigQuery Dataset {dataset_id}: {e}"
                        logging.warning(msg)
                        actions.append(f"[WARN] {msg}")

                dataset_tasks.append(_delete_dataset)
        _run_concurrent(dataset_tasks)
    except Exception as e:
        msg = f"BigQuery dataset list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

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
        ("storage", lambda: handle_storage(project_id, prefix, do_cleanup)),
        ("bigquery", lambda: handle_bigquery(project_id, prefix, do_cleanup)),
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
