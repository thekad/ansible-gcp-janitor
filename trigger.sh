#!/bin/bash
set -euo pipefail

# Configurable defaults (override via environment variables if needed)
FUNCTION_NAME="${FUNCTION_NAME:-ansible-janitor}"
REGION="${REGION:-us-central1}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"

if [ -z "$PROJECT_ID" ]; then
    echo "Error: GCP project ID not found. Set PROJECT_ID environment variable or run 'gcloud config set project <id>'." >&2
    exit 1
fi

echo "Retrieving HTTP endpoint for function '${FUNCTION_NAME}' in region '${REGION}'..."
FUNCTION_URL=$(gcloud functions describe "${FUNCTION_NAME}" \
        --gen2 \
        --region="${REGION}" \
        --project="${PROJECT_ID}" \
        --format="value(serviceConfig.uri)" \
    )

if [ -z "$FUNCTION_URL" ]; then
    echo "Error: Failed to obtain URL for function '${FUNCTION_NAME}'." >&2
    exit 1
fi

echo "Invoking: ${FUNCTION_URL}"
echo "----------------------------------------------------"

# Obtain identity token and trigger HTTP function
RESPONSE=$(curl -s -X POST "${FUNCTION_URL}" \
        -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
        -H "Content-Type: application/json" \
    )

# Pretty-print JSON response if jq is available
if command -v jq &> /dev/null; then
    echo "${RESPONSE}" | jq .
else
    echo "${RESPONSE}"
fi
