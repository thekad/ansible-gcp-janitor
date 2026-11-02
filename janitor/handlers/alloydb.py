from __future__ import annotations

import logging
import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix


def handle_alloydb(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud import alloydb_v1

    client = alloydb_v1.AlloyDBAdminClient()
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    # 1. Backups
    try:
        backup_tasks: list[T.Callable[[], None]] = []
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

    # 2. Clusters: instances are deleted concurrently first (they are slow),
    # then force=True on the cluster cascades to whatever is left (users).
    try:
        instance_tasks: list[T.Callable[[], None]] = []
        cluster_tasks: list[T.Callable[[], None]] = []
        for cluster in client.list_clusters(parent=parent):
            if not (
                match_prefix(cluster.name, prefix)
                or match_prefix(cluster.display_name, prefix)
            ):
                continue
            found.append({"type": "alloydb.Cluster", "name": cluster.name})

            for instance in client.list_instances(parent=cluster.name):
                found.append({"type": "alloydb.Instance", "name": instance.name})
                if do_cleanup:

                    def _delete_instance(instance=instance) -> None:
                        client.delete_instance(name=instance.name).result()
                        actions.append(f"Deleted AlloyDB Instance: {instance.name}")

                    instance_tasks.append(_delete_instance)

            if do_cleanup:

                def _delete_cluster(cluster=cluster) -> None:
                    client.delete_cluster(
                        request=alloydb_v1.DeleteClusterRequest(
                            name=cluster.name, force=True
                        )
                    ).result()
                    actions.append(f"Deleted AlloyDB Cluster: {cluster.name}")

                cluster_tasks.append(_delete_cluster)

        # Instances must be fully deleted before their parent cluster;
        # clusters themselves are independent of each other.
        _run_concurrent(instance_tasks)
        _run_concurrent(cluster_tasks)
    except Exception as e:
        msg = f"AlloyDB cluster list failed: {e}"
        logging.warning(msg)
        actions.append(f"[WARN] {msg}")

    return {"found": found, "actions": actions}
