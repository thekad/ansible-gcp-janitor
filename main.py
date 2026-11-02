from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

import functions_framework

from janitor.config import DEFAULT_PREFIX, DEFAULT_REGION
from janitor.handlers.alloydb import handle_alloydb
from janitor.handlers.bigquery import handle_bigquery
from janitor.handlers.cloudbuild import handle_cloudbuild
from janitor.handlers.cloudbuildv2 import handle_cloudbuildv2
from janitor.handlers.colab import handle_colab
from janitor.handlers.compute import handle_compute
from janitor.handlers.storage import handle_storage
from janitor.handlers.vertexai import handle_vertexai

if TYPE_CHECKING:
    from flask import Request


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
