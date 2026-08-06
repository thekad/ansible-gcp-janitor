# ansible-gcp-janitor

A GCP Cloud Function (2nd Gen) that scans for and optionally deletes leftover GCP
resources from Ansible integration test runs. Resources are identified by a name
prefix (default: `ansible-test-`) and deleted in dependency order so that parent
resources are not torn down before their children.

## How it works

The function exposes a single HTTP endpoint. When invoked it runs a series of
domain-scoped scanners in a fixed order. In **dry-run mode** (the default) it
only lists what it finds. In **cleanup mode** it deletes everything it finds,
waiting for each operation to complete before moving to the next.

### Deletion order

Scanners run in this order to respect cross-domain dependencies (e.g. AlloyDB
clusters hold VPC peerings that would block network deletion):

1. **AlloyDB** — Backups → Instances → Users → Clusters
2. **Cloud Build** — Build Triggers
3. **Cloud Build v2 / Developer Connect** — Git Repository Links → Connections
4. **Colab / Vertex AI Notebooks** — Schedules → Execution Jobs → Runtimes → Runtime Templates
5. **Vertex AI** — Endpoints (models undeployed first) → Datasets → Deployment Resource Pools → Index Endpoints (indexes undeployed first) → Indexes → Metadata Stores → Reasoning Engines → Tensorboards → Featurestores → Feature Online Stores → Feature Groups
6. **Compute** — Instances → Disks → Firewall Rules → Subnetworks → Addresses (regional + global) → VPC Peerings → Networks

## Prerequisites

- `gcloud` CLI authenticated and pointed at your target project
- `jq` (optional, for pretty-printed output from `trigger.sh`)

## Setup

Run once per project to enable APIs and create the `janitor-bot` service account
with the required IAM roles:

```bash
gcloud config set project YOUR_PROJECT_ID
./setup.sh
```

IAM roles granted to the service account:

| Role | Purpose |
|---|---|
| `roles/aiplatform.admin` | Delete Vertex AI and Colab resources |
| `roles/alloydb.admin` | Delete AlloyDB clusters and instances |
| `roles/cloudasset.viewer` | Enumerate project assets |
| `roles/compute.networkAdmin` | Delete instances, disks, networks, addresses |
| `roles/run.invoker` | Allow Cloud Scheduler to invoke the function |
| `roles/servicenetworking.networksAdmin` | Remove PSA/service networking peerings |
| `roles/cloudkms.admin` | Destroy KMS key versions |

## Deployment

```bash
# Dry-run mode (default) — lists resources, deletes nothing
./deploy.sh

# Cleanup mode — actively deletes matched resources
CLEANUP=true ./deploy.sh

# Also create/update the daily Cloud Scheduler job (runs at 02:00 America/Los_Angeles)
CRON=true ./deploy.sh
CRON=true CLEANUP=true ./deploy.sh
```

The function is deployed as `ansible-janitor` in `us-central1` with a 9-minute
timeout and no public access. The Cloud Scheduler job is named
`daily-ansible-janitor`.

## Configuration

All parameters have defaults and can be overridden via environment variables set
at deploy time, or via query parameters at invocation time (query params take
lower priority than env vars).

| Parameter | Env var | Query param | Default |
|---|---|---|---|
| Target project | `TARGET_GCP_PROJECT_ID` | *(env only)* | *(required)* |
| Region | `REGION` | `region` | `us-central1` |
| Name prefix filter | `PREFIX` | `prefix` | `ansible-test-` |
| Active deletion | `DO_CLEANUP` | `do_cleanup` | `false` |

## Manual invocation

```bash
# Dry-run using the deployed function
./trigger.sh

# Override region or prefix at invocation time
REGION=us-east1 ./trigger.sh

# Dry-run with a custom prefix, using curl directly
FUNCTION_URL=$(gcloud functions describe ansible-janitor \
    --gen2 --region=us-central1 --format="value(serviceConfig.uri)")

curl -s "${FUNCTION_URL}?prefix=my-test-" \
    -H "Authorization: Bearer $(gcloud auth print-identity-token)" | jq .
```

## Response format

```json
{
  "mode": "DRY-RUN (Discovery Only)",
  "project": "my-project",
  "region": "us-central1",
  "prefix_filter": "ansible-test-",
  "found_count": 3,
  "found_resources": [
    { "type": "alloydb.Cluster", "name": "projects/.../clusters/ansible-test-db" },
    { "type": "compute.Address", "name": "ansible-test-ip", "scope": "global" },
    { "type": "compute.Network", "name": "ansible-test-vpc" }
  ],
  "actions_taken": []
}
```

In cleanup mode `actions_taken` contains one entry per completed deletion or
peering removal.

## Known gaps

The following resource types are not yet handled and may need manual cleanup:

- GKE clusters (and node pools, which hold subnetworks)
- Cloud SQL instances
- Filestore instances
- Pub/Sub topics and subscriptions
- Secret Manager secrets
