from __future__ import annotations

import concurrent.futures
from typing import Callable, TypedDict

from janitor.config import MAX_DELETE_WORKERS


class HandlerResult(TypedDict):
    found: list[dict[str, str]]
    actions: list[str]


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
