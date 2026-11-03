# ansible-gcp-janitor

A GCP Cloud Function (2nd Gen) that scans for and optionally deletes leftover GCP
resources from Ansible integration test runs. Resources are identified by a name
prefix (default: `ansible-test-`) and deleted in dependency order so that parent
resources are not torn down before their children.

## How it works

The function exposes a single HTTP endpoint. When invoked it runs a series of
domain-scoped scanners in a fixed order. In **dry-run mode** (the default) it
only lists what it finds. In **cleanup mode** it deletes everything it finds.

Within each resource type, deletions run concurrently through a bounded thread
pool (see `janitor/config.py` for `MAX_DELETE_WORKERS`), since each delete blocks
on a GCP long-running operation. Deletions that have cross-dependencies (e.g. child
resources before their parent) are still ordered: each concurrent batch is awaited
in full before the dependent batch begins.

### Deletion order

Scanners run in this order to respect cross-domain dependencies (e.g. AlloyDB
clusters hold VPC peerings that would block network deletion):

1. **AlloyDB** — Backups → Instances → Users → Clusters
2. **Cloud Build** — Build Triggers
3. **Cloud Build v2** — Repositories → Connections
4. **Colab / Vertex AI Notebooks** — Schedules → Execution Jobs → Runtimes →
   Runtime Templates
5. **Vertex AI** — Models (undeployed from all endpoints first) → Endpoints
   (remaining models undeployed first) → Pipeline Jobs → Training Pipelines →
   Batch Prediction Jobs → Custom Jobs → Hyperparameter Tuning Jobs (each
   cancelled if non-terminal, then deleted) → Datasets → Deployment Resource
   Pools → Index Endpoints (indexes undeployed first) → Indexes → Metadata
   Store children (Artifacts → Contexts → Executions) → Metadata Stores →
   Reasoning Engines → Tensorboards → Featurestores → Feature Online Stores →
   Feature Groups
6. **Storage** — Buckets (force-emptied before delete; cascades blobs/versions,
   ≤256 objects)
7. **BigQuery** — Datasets (cascades tables, views, routines, BQML models via
   `delete_contents=True`)
8. **Pub/Sub** — Subscriptions → Topics
9. **Compute** — Instances → Disks → Firewall Rules → Subnetworks → Service
   Networking Connections → Addresses (regional + global) → Networks (VPC
   Peerings removed inline before each network)

Storage, BigQuery, and Pub/Sub have no VPC dependencies and can run anywhere
before Compute. Compute runs last because other domains create Private Service
Access peerings and other VPC attachments that block network deletion.

## Project layout

The function is organized as a modular Python package:

```
main.py                      # HTTP entrypoint; imports handlers and orchestrates deletion order
janitor/
  __init__.py               # Package marker (empty)
  config.py                 # Global constants (DEFAULT_REGION, DEFAULT_PREFIX, MAX_DELETE_WORKERS)
  core.py                   # Shared utilities (HandlerResult, match_prefix, underscore_prefix, _run_concurrent)
  handlers/
    __init__.py             # Package marker (empty)
    compute.py              # Compute Engine resources
    alloydb.py              # AlloyDB clusters, instances, users, backups
    cloudbuild.py           # Cloud Build triggers
    cloudbuildv2.py         # Cloud Build v2 connections and repositories
    colab.py                # Colab/Vertex AI Notebooks
    vertexai.py             # Vertex AI endpoints, indexes, datasets, metadata stores, etc.
    storage.py              # Cloud Storage buckets
    bigquery.py             # BigQuery datasets
```

Each handler module imports only the shared utilities it needs from `janitor/core.py`
and defers all Google Cloud SDK imports to function scope (preserving lazy loading
and baseline memory efficiency). The `scanners` list in `main.py` defines the
order-sensitive deletion sequence across domains.

For detailed documentation on handler architecture, deletion order rationale, and
adding new resource types, see the [AGENTS.md](AGENTS.md) file.

## Prerequisites

- `gcloud` CLI authenticated and pointed at your target project
- `jq` (optional, for pretty-printed output from `trigger.sh`)

## Setup

Run once per project to enable APIs and create the `janitor-bot` service account
with the required IAM roles:

```bash
gcloud config set project YOUR_PROJECT_ID
./scripts/setup.sh
```

See `scripts/setup.sh` for the authoritative list of enabled APIs and IAM roles
assigned to the service account.

## Deployment

```bash
# Dry-run mode (default) — lists resources, deletes nothing
./scripts/deploy.sh

# Cleanup mode — actively deletes matched resources
DO_CLEANUP=true ./scripts/deploy.sh

# Also create/update the daily Cloud Scheduler job (runs at 02:00 America/Los_Angeles)
DO_CRON=true ./scripts/deploy.sh
DO_CRON=true DO_CLEANUP=true ./scripts/deploy.sh
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
./scripts/trigger.sh

# Override region or prefix at invocation time
REGION=us-east1 ./scripts/trigger.sh

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
    { "type": "storage.Bucket", "name": "ansible-test-bucket" },
    { "type": "bigquery.Dataset", "name": "ansible_test_dataset" },
    { "type": "compute.Address", "name": "ansible-test-ip", "scope": "global" },
    { "type": "compute.Network", "name": "ansible-test-vpc" }
  ],
  "actions_taken": []
}
```

In cleanup mode `actions_taken` contains one entry per completed deletion or
peering removal.

## Logging

You can check the latest cloudrun function logs with:

```bash
./scripts/cloudrun-logs.sh [since]
```

Where `since` defaults to "1 day ago"

Or you can check the latest schedule logs with:

```bash
./scripts/schedule-logs.sh [since]
```

## Known gaps

The following resource types are not yet handled and may need manual cleanup:

- GKE clusters (and node pools, which hold subnetworks)
- Cloud SQL instances
- Filestore instances
- Secret Manager secrets
