from __future__ import annotations

import logging
import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix


def handle_storage(project: str, prefix: str, do_cleanup: bool) -> HandlerResult:
    from google.cloud import storage

    client = storage.Client(project=project)
    found: list[dict[str, str]] = []
    actions: list[str] = []

    try:
        bucket_tasks: list[T.Callable[[], None]] = []
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
