from __future__ import annotations

import logging
from typing import Callable

from janitor.core import HandlerResult, match_prefix, _run_concurrent


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
