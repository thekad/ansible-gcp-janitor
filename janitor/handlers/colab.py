from __future__ import annotations

import typing as T

from janitor.core import HandlerResult, _run_concurrent, match_prefix


def handle_colab(
    project: str, region: str, prefix: str, do_cleanup: bool
) -> HandlerResult:
    from google.cloud import aiplatform_v1

    opts = {"api_endpoint": f"{region}-aiplatform.googleapis.com"}
    parent = f"projects/{project}/locations/{region}"
    found, actions = [], []

    # 1. Schedules
    sched_client = aiplatform_v1.ScheduleServiceClient(client_options=opts)
    schedule_tasks: list[T.Callable[[], None]] = []
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

    job_tasks: list[T.Callable[[], None]] = []
    for job in nb_client.list_notebook_execution_jobs(parent=parent):
        if match_prefix(job.name, prefix) or match_prefix(job.display_name, prefix):
            found.append({"type": "colab.NotebookExecution", "name": job.name})
            if do_cleanup:

                def _delete_job(job=job) -> None:
                    nb_client.delete_notebook_execution_job(name=job.name).result()
                    actions.append(f"Deleted Colab Notebook Execution Job: {job.name}")

                job_tasks.append(_delete_job)
    _run_concurrent(job_tasks)

    runtime_tasks: list[T.Callable[[], None]] = []
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

    template_tasks: list[T.Callable[[], None]] = []
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
