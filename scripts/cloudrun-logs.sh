#!/bin/bash
set -euo pipefail

REL_TIME="$*"

if [ -z "$REL_TIME" ];
then
    REL_TIME="1 day ago"
fi

SINCE="$(date --date="${REL_TIME}" --iso-8601=seconds)"

gcloud logging \
    read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"ansible-janitor\" AND timestamp>=\"${SINCE}\"" \
    --order=ASC \
    --format="value(timestamp, textPayload)"
