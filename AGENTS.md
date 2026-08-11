# AGENTS.md — ansible-gcp-janitor

Operational reference for agents working on this codebase.

## What this project does

HTTP-triggered GCP Cloud Function (2nd Gen, Python 3.14) that finds and deletes
leftover GCP resources from Ansible integration tests. Resources are matched by
name prefix and deleted in a strict dependency order to avoid API errors from
attempting to delete parents before children or networks before their consumers.

## Local development environment

A virtualenv lives at `.venv/` in the project root. Always use it for syntax
checks, import validation, and any local execution.

```bash
# First-time setup (or after a fresh clone)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# After any change to requirements.txt
.venv/bin/pip install -r requirements.txt

# Syntax check main.py
.venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('syntax OK')"

# Verify all imports resolve
.venv/bin/python -c "
from google.cloud import compute_v1, alloydb_v1, aiplatform_v1
from google.cloud.devtools import cloudbuild_v1, cloudbuild_v2
import functions_framework
print('imports OK')
"
```

The `.venv/` directory is listed in `.gitignore` and must never be committed.
Always re-run `.venv/bin/pip install -r requirements.txt` after pulling changes if
`requirements.txt` has been modified.

## File map

| File | Purpose |
|---|---|
| `main.py` | All function logic. One handler function per GCP domain. |
| `requirements.txt` | Python dependencies (pinned to major versions). |
| `setup.sh` | One-time project setup: enables APIs, creates service account, grants IAM roles. |
| `deploy.sh` | Deploys the Cloud Function and optionally creates the Cloud Scheduler job. |
| `trigger.sh` | Manually invokes the deployed function using `gcloud auth print-identity-token`. |

## `main.py` structure

```
match_prefix(name: str, prefix: str) -> bool                                          # Strips resource path, checks prefix
handle_compute(project, region, prefix, do_cleanup) -> HandlerResult                  # Compute Engine resources
handle_alloydb(project, region, prefix, do_cleanup) -> HandlerResult                  # AlloyDB clusters, instances, users, backups
handle_cloudbuild(project, prefix, do_cleanup) -> HandlerResult                       # Cloud Build triggers
handle_cloudbuildv2(project, region, prefix, do_cleanup) -> HandlerResult             # Cloud Build v2 connections and repositories
handle_colab(project, region, prefix, do_cleanup) -> HandlerResult                   # Colab/Notebook schedules, runtimes, templates
handle_vertexai(project, region, prefix, do_cleanup) -> HandlerResult                 # Vertex AI endpoints, indexes, datasets, etc.
scan_and_clean_resources(request: Request) -> tuple[str, int] | tuple[str, int, dict] # HTTP entrypoint — orchestrates all handlers
```

`HandlerResult` is a `TypedDict` defined at the top of `main.py`:
```python
class HandlerResult(TypedDict):
    found: list[dict[str, str]]
    actions: list[str]
```

## Deletion order (must be preserved)

The `scanners` list in `scan_and_clean_resources` is **order-sensitive**. Do not
reorder without understanding the dependency chain.

```
alloydb → cloudbuild → cloudbuildv2 → colab → vertexai → compute
```

**Why compute is last:** AlloyDB clusters create Private Service Access (PSA)
peerings on VPC networks. If compute runs first and tries to delete a network
while a PSA peering is still active, the GCP API will return a 400. All VPC
consumers must be deleted before `handle_compute` runs.

**Within `handle_compute`, order is also fixed:**

```
Instances → Disks → Firewall Rules → Subnetworks → Addresses → (VPC Peerings) → Networks
```

- Firewall rules, subnetworks, and addresses must be deleted before their parent network.
- VPC peerings are removed inline per-network (not a separate step) immediately before the network delete call.
- Addresses: `aggregated_list` returns both regional (`regions/{region}` key) and global (`global` key) addresses. Regional addresses use `AddressesClient.delete(region=...)`. Global addresses use `GlobalAddressesClient.delete()` (no region arg). Getting this wrong causes silent misses or "not found" errors.

**Within `handle_vertexai`, order is also fixed for indexes:**

```
Endpoints (undeploy models first) → ... → IndexEndpoints (undeploy indexes first) → Indexes → ...
```

IndexEndpoints must be deleted before backing Indexes, and deployed indexes must
be undeployed from the endpoint before the endpoint itself can be deleted.

## Adding a new resource type

1. If it belongs to an existing domain handler, add it inside that function in
   the correct position relative to its dependencies.
2. If it is a new domain, add a new `handle_<domain>(...)` function following the
   same typed signature and return type:
   ```python
   def handle_<domain>(project: str, region: str, prefix: str, do_cleanup: bool) -> HandlerResult:
   ```
   Then insert it into the `scanners` list **before `compute`** unless it has no
   VPC dependency.
3. Use lazy imports inside the handler (`from google.cloud import ...`) — do not
   add top-level imports.
4. Add the corresponding package to `requirements.txt` pinned to a major version
   (`==N.*`).
5. If the new domain uses a VPC or PSA peering, it **must** appear before
   `compute` in the scanners list.
6. All functions must carry complete type hints — parameters and return type.
   Use `HandlerResult` for domain handlers. For inner helper functions, annotate
   `client_cls: type` and include an explicit `-> None` if the function has no
   return value. Never use bare `dict` or `list` as a return type where a more
   precise type is available.

## Configuration reference

| Env var | Required | Default | Notes |
|---|---|---|---|
| `TARGET_GCP_PROJECT_ID` | Yes | — | Set at deploy time in `deploy.sh` |
| `REGION` | No | `us-central1` | Also overridable via `?region=` query param |
| `PREFIX` | No | `ansible-test-` | Also overridable via `?prefix=` query param |
| `DO_CLEANUP` | No | `false` | Set to `true` to enable active deletion |

Env vars take precedence over query params.

## Dry-run vs. cleanup

The `do_cleanup` boolean is threaded through every handler. All deletion calls
are gated on `if do_cleanup:`. In dry-run mode the function is fully safe to run
against a production project — it only reads and lists.

## Error handling conventions

- Each domain scanner is wrapped in a `try/except` at the top level in
  `scan_and_clean_resources`. A failure in one domain does not abort the others.
- Within handlers, sub-resource listing failures (e.g. AlloyDB API not enabled)
  use `logging.warning` and continue.
- VPC peering removal failures use `logging.warning` and do not abort the
  subsequent network deletion attempt.
- All GCP delete calls use `.result()` to block until the long-running operation
  completes before proceeding to the next resource.

## Known gaps (not yet implemented)

- GKE clusters — node pools hold subnetworks; must be deleted before subnets
- Cloud SQL instances
- Filestore instances
- Pub/Sub topics and subscriptions
- Secret Manager secrets

If adding GKE support: GKE clusters must be added to a new `handle_gke` handler
and inserted into the scanners list **before `compute`**, since node pools consume
subnetworks and their deletion must complete before subnet deletion is attempted.

## Deployment

```bash
# First-time setup (once per project)
./setup.sh

# Deploy in dry-run mode
./deploy.sh

# Deploy in cleanup mode + create scheduler job
DO_CLEANUP=true DO_CRON=true ./deploy.sh
```

The scheduler job runs daily at 02:00 America/Los_Angeles (`0 2 * * *`).
Function timeout is 540 seconds (9 minutes, the Cloud Run Gen2 maximum).
