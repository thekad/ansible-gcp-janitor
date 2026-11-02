from __future__ import annotations

import logging
import time
import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix


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
    instance_tasks: list[T.Callable[[], None]] = []
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
    disk_tasks: list[T.Callable[[], None]] = []
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
    firewall_tasks: list[T.Callable[[], None]] = []
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
    subnet_tasks: list[T.Callable[[], None]] = []
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
        connection_tasks: list[T.Callable[[], None]] = []
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
    address_tasks: list[T.Callable[[], None]] = []
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
    network_tasks: list[T.Callable[[], None]] = []
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
