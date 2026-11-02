#!/bin/bash
set -euo pipefail

REL_TIME="$*"

if [ -z "$REL_TIME" ];
then
    REL_TIME="1 day ago"
fi

SINCE="$(date --date="${REL_TIME}" --iso-8601=seconds)"

gcloud logging \
    read "resource.type=\"cloud_scheduler_job\" AND resource.labels.job_id=\"daily-ansible-janitor\" AND timestamp>=\"${SINCE}\"" \
    --order=ASC \
    --format="value(timestamp, severity, jsonPayload)"
