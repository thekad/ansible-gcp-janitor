from __future__ import annotations

import logging
import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix


def handle_cloudbuildv2(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud.devtools import cloudbuild_v2

    client = cloudbuild_v2.RepositoryManagerClient()
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    try:
        repo_tasks: list[T.Callable[[], None]] = []
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

        connection_tasks: list[T.Callable[[], None]] = []
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
