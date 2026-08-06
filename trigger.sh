#!/bin/bash
set -euo pipefail

# --- Function location (where the Cloud Function is deployed) ---
FUNCTION_NAME="${FUNCTION_NAME:-ansible-janitor}"
FUNCTION_REGION="${FUNCTION_REGION:-us-central1}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"

# --- Scan parameters (passed as query params to the function) ---
# SCAN_REGION: which GCP region to scan for resources (defaults to function region)
SCAN_REGION="${SCAN_REGION:-${FUNCTION_REGION}}"
# DO_CLEANUP: set to "true" to enable active deletion; any other value = dry-run
DO_CLEANUP="${DO_CLEANUP:-false}"
# PREFIX: name prefix filter (leave unset to use the function's default)
PREFIX="${PREFIX:-}"

if [ -z "$PROJECT_ID" ]; then
    echo "Error: GCP project ID not found. Set PROJECT_ID or run 'gcloud config set project <id>'." >&2
    exit 1
fi

echo "Retrieving HTTP endpoint for function '${FUNCTION_NAME}' in region '${FUNCTION_REGION}'..."
FUNCTION_URL=$(gcloud functions describe "${FUNCTION_NAME}" \
        --gen2 \
        --region="${FUNCTION_REGION}" \
        --project="${PROJECT_ID}" \
        --format="value(serviceConfig.uri)" \
    )

if [ -z "$FUNCTION_URL" ]; then
    echo "Error: Failed to obtain URL for function '${FUNCTION_NAME}'." >&2
    exit 1
fi

# Build query string
QUERY="region=${SCAN_REGION}&do_cleanup=${DO_CLEANUP}"
if [ -n "${PREFIX}" ]; then
    QUERY="${QUERY}&prefix=${PREFIX}"
fi

echo "Mode:        $([ "${DO_CLEANUP}" = "true" ] && echo "ACTIVE DELETION" || echo "dry-run")"
echo "Scan region: ${SCAN_REGION}"
echo "Prefix:      ${PREFIX:-<function default>}"
echo "Invoking:    ${FUNCTION_URL}?${QUERY}"
echo "----------------------------------------------------"

RESPONSE=$(curl -s -X GET "${FUNCTION_URL}?${QUERY}" \
        -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
        -H "Content-Type: application/json" \
    )

if command -v jq &> /dev/null; then
    echo "${RESPONSE}" | jq .
else
    echo "${RESPONSE}"
fi
