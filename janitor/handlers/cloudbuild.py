from __future__ import annotations

import logging
from typing import Callable

from janitor.core import HandlerResult, match_prefix, _run_concurrent


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
