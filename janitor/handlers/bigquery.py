from __future__ import annotations

import logging
import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix, underscore_prefix


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
        dataset_tasks: list[T.Callable[[], None]] = []
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
