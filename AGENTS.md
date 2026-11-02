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

# Syntax check and bytecode compilation for all modules
.venv/bin/python -m compileall -q main.py janitor

# Verify all imports resolve (Google SDKs are lazy-loaded, only project module imports checked)
.venv/bin/python -c "
import sys, main
sdk = sorted(m for m in sys.modules if m.startswith(('google.cloud.compute', 'google.cloud.alloydb', 'google.cloud.aiplatform', 'google.cloud.bigquery', 'google.cloud.storage', 'google.cloud.devtools', 'google.cloud.resourcemanager', 'google.auth', 'google_auth_httplib2', 'googleapiclient', 'httplib2')))
assert not sdk, sdk
print('imports OK, lazy loading preserved')
"
```

The `.venv/` directory is listed in `.gitignore` and must never be committed.
The `uv.lock` file captures exact dependency versions for reproducible builds and
**should be committed** to ensure consistent deployments across environments.
Always re-run `uv sync` after pulling changes if `pyproject.toml` has been modified.

## File map

| File | Purpose |
|---|---|
| `pyproject.toml` | Project metadata and dependencies (uv-managed). |
| `uv.lock` | Pinned dependency versions for reproducible builds (committed to repo). |

**Legacy files (no longer used):**

| File | Status |
|---|---|
| `requirements.txt` | Deprecated; replaced by `pyproject.toml` and `uv.lock`. |

---

### Remaining files

| File | Purpose |
|---|---|
| `main.py` | HTTP entrypoint; imports handlers and orchestrates cross-domain deletion order. |
| `janitor/config.py` | Global constants: `DEFAULT_REGION`, `DEFAULT_PREFIX`, `MAX_DELETE_WORKERS`. |
| `janitor/core.py` | Shared utilities: `HandlerResult` TypedDict, `match_prefix()`, `underscore_prefix()`, `_run_concurrent()`. |
| `janitor/handlers/` | Package containing domain-specific handler modules. |
| `janitor/handlers/*.py` | Individual handlers: `compute.py`, `alloydb.py`, `cloudbuild.py`, `cloudbuildv2.py`, `colab.py`, `vertexai.py`, `storage.py`, `bigquery.py`. |
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
alloydb → cloudbuild → cloudbuildv2 → colab → vertexai → storage → bigquery → compute
```

**Why compute is last:** AlloyDB clusters create Private Service Access (PSA)
peerings on VPC networks. If compute runs first and tries to delete a network
while a PSA peering is still active, the GCP API will return a 400. All VPC
consumers must be deleted before `handle_compute` runs.

**BigQuery has no VPC dependencies** and can sit anywhere before `compute`. It
uses synchronous delete calls (no long-running operations), and
`delete_dataset(..., delete_contents=True)` cascades to all contained tables,
views, routines, and BQML models in one call.

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
| **Vertex AI Featurestore** | `force=True` | All EntityTypes and Features |
| **Vertex AI FeatureOnlineStore** | `force=True` | All FeatureViews |
| **Vertex AI FeatureGroup** | `force=True` | All Features |

**Storage bucket limitation:** The `force=True` parameter only works for buckets
containing ≤256 objects. Buckets exceeding this limit will fail with a
`ValueError`. Ansible integration tests create buckets with a handful of items,
well under this threshold.

Resources **not** using cascade delete (explicit child deletion required):

- **AlloyDB Cluster** → Instances, Users (deleted explicitly before cluster)
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
update dependencies via `uv sync` after modifying `pyproject.toml`.
