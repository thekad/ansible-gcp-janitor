# AGENTS.md — ansible-gcp-janitor

Operational reference for agents working on this codebase.

## What this project does

HTTP-triggered GCP Cloud Function (2nd Gen, Python 3.14) that finds and deletes
leftover GCP resources from Ansible integration tests. Resources are matched by
name prefix and deleted in a strict dependency order to avoid API errors from
attempting to delete parents before children or networks before their consumers.

## Local development environment

A virtual environment lives at `.venv/` in the project root, managed by `uv`.
Always use it for syntax checks, import validation, and any local execution.

```bash
# First-time setup (or after a fresh clone)
uv sync

# After any change to pyproject.toml
uv sync

# Install test dependencies (for running tests locally)
uv sync --extra dev

# Syntax check and bytecode compilation for all modules
uv run python -m compileall -q main.py janitor

# Verify all imports resolve (Google SDKs are lazy-loaded, only project module imports checked)
uv run python -c "
import sys, main
sdk = sorted(m for m in sys.modules if m.startswith(('google.cloud.compute', 'google.cloud.alloydb', 'google.cloud.aiplatform', 'google.cloud.bigquery', 'google.cloud.storage', 'google.cloud.devtools', 'google.cloud.pubsub', 'google.cloud.resourcemanager', 'google.auth', 'google_auth_httplib2', 'googleapiclient', 'httplib2')))
assert not sdk, sdk
print('imports OK, lazy loading preserved')
"

# Run the test suite
uv run pytest tests/unit/ -v
```

The `.venv/` directory is listed in `.gitignore` and must never be committed.
The `uv.lock` file captures exact dependency versions for reproducible builds and
**should be committed** to ensure consistent deployments across environments.
Always re-run `uv sync` after pulling changes if `pyproject.toml` has been modified.
After running `uv sync --extra dev`, test dependencies are available and you can run
`uv run pytest` to verify all changes.

## File map

| File | Purpose |
|---|---|
| `pyproject.toml` | Project metadata and dependencies (uv-managed). |
| `uv.lock` | Pinned dependency versions for reproducible builds (committed to repo). |
| `TESTING.md` | Comprehensive testing guide. |
| `tests/` | Test suite (unit, handler-specific). |

**Legacy files (no longer used):**

| File | Status |
|---|---|
| `requirements.txt` | Deprecated; replaced by `pyproject.toml` and `uv.lock`. |

---

### Remaining files

| File | Purpose |
|---|---|
| `main.py` | HTTP entrypoint; imports handlers and orchestrates cross-domain deletion order. |
| `janitor/config.py` | Global constants: region, prefix, thread pool size. |
| `janitor/core.py` | Shared utilities: utilities for matching, filtering, and concurrent operations. |
| `janitor/handlers/` | Package containing domain-specific resource deletion handlers. |
| `janitor/handlers/*.py` | Individual handlers: compute, alloydb, cloudbuild, cloudbuildv2, colab, vertexai, pubsub, storage, bigquery. |
| `scripts/setup.sh` | One-time project setup: enables APIs, creates service account, grants IAM roles. |
| `scripts/deploy.sh` | Deploys the Cloud Function and optionally creates the Cloud Scheduler job. |
| `scripts/trigger.sh` | Manually invokes the deployed function using `gcloud auth print-identity-token`. |

## Package structure

### `janitor/config.py`
```python
DEFAULT_REGION: str              # "us-central1"
DEFAULT_PREFIX: str              # "ansible-test-"
MAX_DELETE_WORKERS: int          # 8 (fixed; thread pool size for concurrent deletions)
```

### `janitor/core.py`
```python
class HandlerResult(TypedDict):  # Return type for all domain handlers
    found: list[dict[str, str]]
    actions: list[str]

match_prefix(name: str, prefix: str) -> bool                           # Strips resource path, checks prefix
underscore_prefix(prefix: str) -> str                                  # Converts dashes to underscores for BigQuery/Vertex AI
_run_concurrent(...)                                                   # ThreadPoolExecutor wrapper for parallel deletions
```

### `janitor/handlers/*.py` (per-domain modules)
```python
handle_compute(project, region, prefix, do_cleanup) -> HandlerResult       # janitor/handlers/compute.py
handle_alloydb(project, region, prefix, do_cleanup) -> HandlerResult       # janitor/handlers/alloydb.py
handle_cloudbuild(project, prefix, do_cleanup) -> HandlerResult            # janitor/handlers/cloudbuild.py
handle_cloudbuildv2(project, region, prefix, do_cleanup) -> HandlerResult  # janitor/handlers/cloudbuildv2.py
handle_colab(project, region, prefix, do_cleanup) -> HandlerResult         # janitor/handlers/colab.py
handle_vertexai(project, region, prefix, do_cleanup) -> HandlerResult      # janitor/handlers/vertexai.py
handle_storage(project, prefix, do_cleanup) -> HandlerResult               # janitor/handlers/storage.py
handle_bigquery(project, prefix, do_cleanup) -> HandlerResult              # janitor/handlers/bigquery.py
handle_pubsub(project, prefix, do_cleanup) -> HandlerResult                # janitor/handlers/pubsub.py
```

### `main.py`
```python
scan_and_clean_resources(request: Request) -> tuple[str, int] | tuple[str, int, dict]
```
HTTP entrypoint. Imports all handlers and defines the `scanners` list (order-sensitive,
dicts domain handler functions in dependency order).

## Deletion order (must be preserved)

The `scanners` list in `main.py`'s `scan_and_clean_resources` function is **order-sensitive**.
Do not reorder without understanding the dependency chain. Cross-domain order is defined
there; within-domain order is defined in each handler module (e.g., within `handle_compute`,
instances are deleted before disks, etc.).

```
alloydb → cloudbuild → cloudbuildv2 → colab → vertexai → storage → bigquery → pubsub → compute
```

**Why compute is last:** AlloyDB clusters create Private Service Access (PSA)
peerings on VPC networks. If compute runs first and tries to delete a network
while a PSA peering is still active, the GCP API will return a 400. All VPC
consumers must be deleted before `handle_compute` runs.

**Storage and BigQuery before PubSub:** PubSub topics may be configured to
publish messages to Cloud Storage buckets or BigQuery datasets. These
destinations must be deleted first to avoid orphaned topic subscriptions or
failed message delivery attempts. Within PubSub, subscriptions are deleted
before their parent topics to avoid API errors.

**Within `handle_compute`, order is also fixed:**

```
Instances → Disks → Firewall Rules → Subnetworks → Addresses → (VPC Peerings) → Networks
```

- Firewall rules, subnetworks, and addresses must be deleted before their parent network.
- VPC peerings are removed inline per-network (not a separate step) immediately before the network delete call.
- Addresses: `aggregated_list` returns both regional (`regions/{region}` key) and global (`global` key) addresses. Regional addresses use `AddressesClient.delete(region=...)`. Global addresses use `GlobalAddressesClient.delete()` (no region arg). Getting this wrong causes silent misses or "not found" errors.

**Within `handle_vertexai`, order is also fixed for indexes and metadata:**

```
Endpoints (undeploy models first) → ... → IndexEndpoints (undeploy indexes first) → Indexes → MetadataStore children (artifacts, contexts, executions) → MetadataStores → ...
```

IndexEndpoints must be deleted before backing Indexes, and deployed indexes must
be undeployed from the endpoint before the endpoint itself can be deleted.

MetadataStores automatically create child resources (artifacts, contexts, executions)
as ML lineage records when Vertex AI Pipelines or AutoML run. These metadata records
remain even after the underlying resources (datasets, models, etc.) are deleted. All
child resources within a MetadataStore must be deleted before the store itself can be
deleted, otherwise the API returns an error.

## Adding a new resource type

1. If it belongs to an existing domain, add it inside that function in
   the correct position relative to its dependencies.
2. If it is a new domain:
   a. Create a new module `janitor/handlers/<domain>.py` with a handler function:
      ```python
      def handle_<domain>(project: str, region: str, prefix: str, do_cleanup: bool) -> HandlerResult:
      ```
   b. Import shared utilities from `janitor.core` (only what is needed):
      ```python
      from janitor.core import HandlerResult, match_prefix, ...
      ```
   c. Keep all Google Cloud SDK imports lazy (inside the handler function body).
   d. Include complete type hints on all functions (see point 5 below).
   e. In `main.py`, import the new handler and add it to the `scanners` list **before
      `compute`** unless it has no VPC dependency.
3. **Lazy imports rule**: Only Google/third-party SDKs are lazy-loaded (inside function
   scope). Standard library imports (`logging`, `time`, `typing`, `concurrent.futures`,
   etc.) are loaded eagerly at module top.
   - **Why**: Cloud Function instances may not need all GCP SDKs loaded. Lazy imports
     keep baseline memory footprint low (~80–120MB) and defer loading only to handlers
     that are actually invoked. This is especially important since the function runs
     daily on a fixed schedule; every MB of memory reduction translates to lower GCP
     Cloud Run costs across the year. Standard library imports are tiny (negligible
     memory overhead) and are always needed, so they are loaded top-level.
4. Add the corresponding package to `requirements.txt` pinned to a major version
   (`==N.*`).
5. If the new domain uses a VPC or PSA peering, it **must** appear before
   `compute` in the scanners list.
6. All functions must carry complete type hints — parameters and return type.
   Use `HandlerResult` for domain handlers. For inner helper functions, annotate
   `client_cls: type` and include an explicit `-> None` if the function has no
   return value. Never use bare `dict` or `list` as a return type where a more
   precise type is available.
   - **Why**: Type hints enable static analysis tools (mypy, pylint) to catch API
     misuse before deployment. They also improve code readability and enable better
     IDE autocomplete. Since this function has strict ordering constraints and
     complex interdependencies, explicit types prevent silent failures (e.g.,
     forgetting to pass `do_cleanup` parameter, returning wrong dict structure).

## Setup steps for new handlers

When adding a new handler domain, you **must** update `scripts/setup.sh` to enable
the required GCP API and assign the necessary IAM role to the service account.
Failure to do so will result in "permission denied" or "API not enabled" errors
at function runtime.

**Steps:**

1. Add the service API to the `gcloud services enable` call. API names follow the
   pattern `<service>.googleapis.com`. For example, for PubSub: `pubsub.googleapis.com`.

2. Add the appropriate IAM role to the `ROLES` array. Role names follow the pattern
   `roles/<service>.<verb>`. For example, for PubSub: `roles/pubsub.admin`.

   To find the correct role, search the
   [GCP IAM Roles documentation](https://cloud.google.com/iam/docs/understanding-custom-roles#predefined_roles)
   or use `gcloud iam roles list --filter='<service>'`.

3. Add a brief comment explaining what the role enables (following the existing pattern).

4. Run `./scripts/setup.sh` to apply changes to an existing project.

5. Update this AGENTS.md file with the API and role added.

**Example:** When adding the PubSub handler:

```bash
# In scripts/setup.sh, add to gcloud services enable:
pubsub.googleapis.com

# In scripts/setup.sh, add to ROLES array:
"roles/pubsub.admin"                    # Delete PubSub topics and subscriptions
```

Then run:
```bash
./scripts/setup.sh
```

## Configuration reference

| Env var | Required | Default | Notes |
|---|---|---|---|
| `TARGET_GCP_PROJECT_ID` | Yes | — | Set at deploy time in `scripts/deploy.sh` |
| `REGION` | No | `us-central1` | Also overridable via `?region=` query param |
| `PREFIX` | No | `ansible-test-` | Also overridable via `?prefix=` query param |
| `DO_CLEANUP` | No | `false` | Set to `true` to enable active deletion |

Env vars take precedence over query params.

## Dry-run vs. cleanup

The `do_cleanup` boolean is threaded through every handler. All deletion calls
are gated on `if do_cleanup:`. In dry-run mode the function is fully safe to run
against a production project — it only reads and lists.

## Cascade delete behavior

Several GCP resources support cascade deletion via `force=True` or similar
parameters. The janitor leverages this to simplify cleanup, reduce API calls,
and speed up execution. Child resources deleted by cascade are not listed
individually in the `found` resources list — only their parent resource appears.

| Resource | Delete Parameter | Child Resources Cascaded |
|----------|------------------|--------------------------|
| **BigQuery Dataset** | `delete_contents=True` | All tables, views, routines, BQML models |
| **Storage Bucket** | `force=True` | All blobs/objects and their versions (max 256 objects) |
| **AlloyDB Cluster** | `force=True` | Users (instances are deleted concurrently first to stay within the timeout) |
| **Vertex AI Featurestore** | `force=True` | All EntityTypes and Features |
| **Vertex AI FeatureOnlineStore** | `force=True` | All FeatureViews |
| **Vertex AI FeatureGroup** | `force=True` | All Features |

**Storage bucket limitation:** The `force=True` parameter only works for buckets
containing ≤256 objects. Buckets exceeding this limit will fail with a
`ValueError`. Ansible integration tests create buckets with a handful of items,
well under this threshold.

Resources **not** using cascade delete (explicit child deletion required):

- **Cloud Build v2 Connection** → Repositories (deleted explicitly before connection)
- **Vertex AI MetadataStore** → Artifacts, Contexts, Executions (deleted explicitly before store)
- **Vertex AI IndexEndpoint** → deployed indexes must be undeployed before endpoint deletion
- **Vertex AI Endpoint** → deployed models must be undeployed before endpoint deletion

These resources require explicit child deletion because their APIs do not provide
a `force` or cascade parameter, or because the undeployment step involves additional
state transitions beyond simple deletion.

## Error handling conventions

- Each domain scanner is wrapped in a `try/except` at the top level in
  `scan_and_clean_resources`. A failure in one domain does not abort the others.
- Within handlers, sub-resource listing failures (e.g. AlloyDB API not enabled)
  use `logging.warning` and continue.
- VPC peering removal failures use `logging.warning` and do not abort the
  subsequent network deletion attempt.
- All GCP delete calls use `.result()` to block until the long-running operation
  completes before proceeding to the next resource.

## APIs and IAM roles enabled by setup.sh

| API | Role | Handler(s) |
|---|---|---|
| `aiplatform.googleapis.com` | `roles/aiplatform.admin` | vertexai, colab |
| `alloydb.googleapis.com` | `roles/alloydb.admin` | alloydb |
| `bigquery.googleapis.com` | `roles/bigquery.admin` | bigquery |
| `cloudasset.googleapis.com` | — | (used by underlying APIs) |
| `cloudbuild.googleapis.com` | `roles/cloudbuild.admin` | cloudbuild |
| `cloudfunctions.googleapis.com` | — | (Cloud Function itself) |
| `cloudresourcemanager.googleapis.com` | — | (used by underlying APIs) |
| `cloudscheduler.googleapis.com` | — | (Cloud Scheduler for recurring invocation) |
| `compute.googleapis.com` | `roles/compute.admin` | compute |
| `pubsub.googleapis.com` | `roles/pubsub.admin` | pubsub |
| `run.googleapis.com` | `roles/run.invoker` | (Cloud Run function execution) |
| `storage.googleapis.com` | `roles/storage.admin` | storage |

Supporting roles:
- `roles/browser` — resolve project ID → project number (Service Networking API)
- `roles/servicenetworking.networksAdmin` — disconnect VPC peerings (compute handler)

## Known gaps (not yet implemented)

- GKE clusters — node pools hold subnetworks; must be deleted before subnets
- Cloud SQL instances
- Filestore instances
- Secret Manager secrets

If adding GKE support: GKE clusters must be added to a new `handle_gke` handler
and inserted into the scanners list **before `compute`**, since node pools consume
subnetworks and their deletion must complete before subnet deletion is attempted.
Do not forget to add `container.googleapis.com` and `roles/container.admin` to
`scripts/setup.sh`.

## Testing

The project includes a comprehensive test suite covering core utilities, HTTP handler logic,
and resource deletion handlers. All tests use mocked GCP clients and run locally without
requiring GCP authentication.

**Install test dependencies:**
```bash
uv sync --extra dev
```

**Run all unit tests:**
```bash
uv run pytest tests/unit/ -v
```

**Run tests with coverage report:**
```bash
uv run pytest tests/ --cov=janitor --cov=main --cov-report=html
```

See `TESTING.md` for detailed testing documentation, including:
- Test structure and organization
- How to mock GCP clients and long-running operations
- Adding new handler tests
- Troubleshooting common mocking issues

## Deployment

```bash
# First-time setup (once per project)
./scripts/setup.sh

# Deploy in dry-run mode
./scripts/deploy.sh

# Deploy in cleanup mode + create scheduler job
DO_CLEANUP=true DO_CRON=true ./scripts/deploy.sh
```

The scheduler job runs daily at 02:00 America/Los_Angeles (`0 2 * * *`).
Function timeout is 540 seconds (9 minutes, the Cloud Run Gen2 maximum).

**Note on dependencies:** Cloud Functions 2nd Gen automatically detects and uses `uv` for
Python 3.11+ runtimes. The `uv.lock` file is committed to ensure reproducible, deterministic
builds across all deployment environments. Never delete or manually edit `uv.lock`;
update dependencies via `uv sync` after modifying `pyproject.toml`. Test dependencies are
installed via `uv sync --extra dev`.
