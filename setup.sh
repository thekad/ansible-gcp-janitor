#!/bin/bash -e
set -euo pipefail

PROJECT_ID=$(gcloud config get-value project)
SA_NAME="janitor-bot"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Configuring GCP Project: ${PROJECT_ID}"

# 1. Enable required APIs
echo "Enabling GCP APIs..."
gcloud services enable \
    aiplatform.googleapis.com \
    alloydb.googleapis.com \
    cloudasset.googleapis.com \
    cloudbuild.googleapis.com \
    cloudfunctions.googleapis.com \
    cloudscheduler.googleapis.com \
    compute.googleapis.com \
    run.googleapis.com

# 2. Create Service Account if it does not exist
if ! gcloud iam service-accounts describe "${SA_EMAIL}" &>/dev/null; then
    echo "Creating service account ${SA_NAME}..."
    gcloud iam service-accounts create "${SA_NAME}" \
        --display-name="Automated Integration Test Janitor"
else
    echo "Service account ${SA_NAME} already exists."
fi

# 3. Assign required IAM Roles
ROLES=(
    "roles/aiplatform.admin"                # Delete Vertex AI models, runtimes, & endpoints
    "roles/alloydb.admin"                   # Delete AlloyDB instances & clusters
    "roles/cloudbuild.admin"                # Destroy cloudbuild triggers
    "roles/compute.admin"                   # Delete instances, disks, firewall rules, addresses, and networks
    "roles/run.invoker"                     # janitor-bot acts as Scheduler's OIDC identity to invoke itself
    "roles/servicenetworking.networksAdmin" # Disconnect service networking connections
)

echo "Binding IAM roles to ${SA_EMAIL}..."
for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="${ROLE}" \
        --condition=None \
        --quiet > /dev/null
    echo " -> Granted ${ROLE}"
done

echo "Setup complete!"
