#!/bin/bash -e
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
FUNCTION_NAME="ansible-janitor"
SA_NAME="janitor-bot"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_JOB_NAME="daily-ansible-janitor"
DO_CRON="${CRON:-'false'}"
CRON_SCHEDULE="0 2 * * *"  # Runs daily at 2:00 AM

# Toggle dry-run mode ("false") vs active destruction ("true") defaults to false
DO_CLEANUP="${CLEANUP:-'false'}"

echo "Deploying Cloud Function (2nd Gen)..."
gcloud functions deploy "${FUNCTION_NAME}" \
    --gen2 \
    --runtime=python314 \
    --region="${REGION}" \
    --memory=512Mi \
    --entry-point=scan_and_clean_resources \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account="${SA_EMAIL}" \
    --set-env-vars TARGET_GCP_PROJECT_ID="${PROJECT_ID}",DO_CLEANUP="${DO_CLEANUP}"

# Extract Function HTTP URL
FUNCTION_URL=$(gcloud functions describe "${FUNCTION_NAME}" \
        --gen2 \
        --region="${REGION}" \
        --format="value(serviceConfig.uri)" \
    )

echo "Deployed Function URL: ${FUNCTION_URL}"

if [ "$DO_CRON" = "true" ]; then
    # Create or Update Cloud Scheduler Job
    echo "Configuring Cloud Scheduler..."
    if gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" --location="${REGION}" &>/dev/null; then
        gcloud scheduler jobs update http "${SCHEDULER_JOB_NAME}" \
            --location="${REGION}" \
            --schedule="${CRON_SCHEDULE}" \
            --uri="${FUNCTION_URL}" \
            --http-method=GET \
            --time-zone=America/Los_Angeles \
            --oidc-service-account-email="${SA_EMAIL}"
    else
        gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
            --location="${REGION}" \
            --schedule="${CRON_SCHEDULE}" \
            --uri="${FUNCTION_URL}" \
            --http-method=GET \
            --time-zone=America/Los_Angeles \
            --oidc-service-account-email="${SA_EMAIL}"
    fi
fi

echo "Deployment finished. Function deployed in DO_CLEANUP=${DO_CLEANUP} mode."
