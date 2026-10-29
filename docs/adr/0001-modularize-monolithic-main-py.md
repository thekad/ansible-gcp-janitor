# ADR 0001: Modularize Monolithic main.py into Package Structure

**Date:** 2026-10-29  
**Status:** PROPOSED  
**Decision Makers:** Project maintainers  

---

## Context

The ansible-gcp-janitor Cloud Function is currently implemented as a single monolithic `main.py` file containing 1,196 lines of code across:

- **8 domain-specific handlers** (compute, alloydb, cloudbuild, cloudbuildv2, colab, vertexai, storage, bigquery)
- **4 shared utilities** (match_prefix, underscore_prefix, _run_concurrent, HandlerResult)
- **3 configuration constants** (DEFAULT_REGION, DEFAULT_PREFIX, MAX_DELETE_WORKERS)
- **1 HTTP entrypoint** (scan_and_clean_resources)

### Current Pain Points

1. **Testability**: Handlers can be imported individually today, but any test module must import the whole 1,196-line file and patch SDK clients inside it; per-domain modules give tests a narrower, clearer target.
2. **Cognitive Load**: Developers must navigate 1,196 lines to locate domain-specific logic; vertical scroll is significant.
3. **Parallel Development**: Concurrent changes to different handlers all touch the same file, increasing merge-conflict risk.
4. **Reusability**: Shared utilities and domain handlers cannot be reused in other projects (e.g., separate cleanup scripts, standalone handler testing).
5. **Maintenance**: Adding new domain handlers requires modifying the monolithic file; no clear pattern for contributors.

### Project Context

- Runtime: Python 3.14 on Google Cloud Functions 2nd Gen
- Deployment: Automated via `deploy.sh`; entry point is `main.py` + function name
- Dependencies: 11 external packages (google-cloud-*, functions-framework, httplib2)
- Concurrency Model: ThreadPoolExecutor with 8 workers for I/O-bound deletion operations
- No existing unit tests; validation occurs at deployment time

---

## Decision

**Refactor main.py into a modular Python package structure** following the layout below:

```
ansible-gcp-janitor/
├── main.py                             # Entry point (remains at root)
├── janitor/                            # NEW: Package root
│   ├── __init__.py                     # Package initialization
│   ├── config.py                       # Constants & defaults (~30 LOC)
│   ├── core.py                         # Shared utilities (~100 LOC)
│   └── handlers/                       # Domain-specific handlers
│       ├── __init__.py                 # Empty package marker
│       ├── compute.py                  # Compute Engine cleanup (~330 LOC)
│       ├── alloydb.py                  # AlloyDB cleanup (~90 LOC)
│       ├── cloudbuild.py               # Cloud Build cleanup (~35 LOC)
│       ├── cloudbuildv2.py             # Cloud Build v2 cleanup (~55 LOC)
│       ├── colab.py                    # Colab cleanup (~70 LOC)
│       ├── vertexai.py                 # Vertex AI cleanup (~400 LOC)
│       ├── storage.py                  # Cloud Storage cleanup (~35 LOC)
│       └── bigquery.py                 # BigQuery cleanup (~50 LOC)
```

Adding a `tests/` suite is a follow-up enabled by this change; it is out of scope here.

Absolute imports (`from janitor.core import ...`) are used throughout, and
`janitor/__init__.py` stays empty (no re-exports), so importing a handler module never
pulls in sibling handlers or any Google SDK.

### Package Responsibilities

| Module | Purpose | Dependencies |
|--------|---------|--------------|
| `janitor/config.py` | Global constants: DEFAULT_REGION, DEFAULT_PREFIX, MAX_DELETE_WORKERS | None (leaf) |
| `janitor/core.py` | HandlerResult TypedDict, match_prefix(), underscore_prefix(), _run_concurrent() | stdlib + `config` (`_run_concurrent` uses `MAX_DELETE_WORKERS`) |
| `janitor/handlers/__init__.py` | Empty package marker | None |
| `janitor/handlers/compute.py` | handle_compute() + nested helper closures | `core` (+ stdlib `logging`, `time`; Google SDKs imported lazily) |
| `janitor/handlers/alloydb.py` | handle_alloydb() + nested helper closures | `core` (+ stdlib `logging`; `alloydb_v1` imported lazily) |
| Similar for other handlers | Each domain isolated | `core` + domain-specific SDK (lazy) |
| `main.py` | @functions_framework.http handler, scanner ordering | `config`, each handler module |

No handler imports `config` directly: `MAX_DELETE_WORKERS` is consumed only by
`core._run_concurrent`, and `DEFAULT_REGION` / `DEFAULT_PREFIX` only by `main.py`.

---

## Rationale

### Why This Decision

1. **No Circular Dependencies**: Dependency graph is acyclic (`main` → handlers → `core` → `config`; handlers never import each other).
2. **Cloud Function Compatibility**: `main.py` remains at project root and `janitor/` is uploaded with the source directory (`.gcloudignore` excludes only `.gitignore` entries, `*.md`, `*.sh` and `node_modules`). `deploy.sh` needs no changes; this is confirmed by the Phase 8 deploy.
3. **Preserves Behavior**: Pure reorganization of existing code; no logic rewrites. All lazy Google SDK imports are retained; stdlib imports may be top-level.
4. **Enables Testing**: Individual handlers become testable with mocked GCP clients.
5. **Reduces Cognitive Load**: Each handler file is 35–400 LOC (vs scanning 1,196-line monolith).
6. **Facilitates Collaboration**: Changes to different handlers touch different files, reducing merge conflicts.
7. **Establishes Pattern**: Clear structure for adding new domains (create a new module under `janitor/handlers/`, then import it and add a `scanners` entry in `main.py`).

### What This Does NOT Do

- Does not change HTTP entrypoint location or signature
- Does not refactor deletion logic (no behavior changes)
- Does not extract configuration into YAML/JSON (possible follow-up, ADR 0002)
- Does not migrate to Go (not planned)
- Does not add runtime dependencies (`ruff` is a local dev-only check)
- Does not change the order-sensitive `scanners` list or any deletion order
- Does not add tests (a follow-up enabled by this change)

---

## Implementation Plan

### Phase 0: Update Documentation (5 minutes)
`README.md` has no project-structure or development section, so add a short
"Project layout" section (after "How it works") describing `main.py` (HTTP entrypoint
and scanner ordering), `janitor/core.py`, `janitor/config.py` and `janitor/handlers/`.
Also update the "How it works" paragraph that references `MAX_DELETE_WORKERS = 8` so it
points to `janitor/config.py`. The deletion-order list does not change.

### Phase 1: Create Package Structure (5 minutes)
```bash
mkdir -p janitor/handlers
touch janitor/__init__.py janitor/handlers/__init__.py   # both stay empty
```

Take a baseline before moving any code (used in Phase 7): run a dry-run against a
project with known leftover resources and save the JSON response.

### Phase 2: Extract janitor/config.py (10 minutes)
Move `DEFAULT_REGION`, `DEFAULT_PREFIX` and `MAX_DELETE_WORKERS` (with the comment
explaining why the worker count is fixed) verbatim. No imports.

### Phase 3: Extract janitor/core.py (10 minutes)
Move `HandlerResult`, `match_prefix()`, `underscore_prefix()` and `_run_concurrent()`
verbatim. Required imports: `from __future__ import annotations`, `concurrent.futures`,
`from typing import Callable, TypedDict`, and `from janitor.config import MAX_DELETE_WORKERS`.
(`logging` is not needed.)

### Phase 4: Extract Handlers (60 minutes)
For each handler (compute, alloydb, cloudbuild, cloudbuildv2, colab, vertexai, storage, bigquery):
1. Create `janitor/handlers/{name}.py`
2. Move `handle_{name}()` and its nested helpers verbatim; do not change logic
3. Start the file with `from __future__ import annotations`
4. Add eager imports only for what the module uses from the standard library
   (e.g. `logging`, `time`, `from typing import Callable`) and from the project
   (`from janitor.core import HandlerResult, match_prefix, underscore_prefix, _run_concurrent`,
   importing only the names actually used)
5. Keep every Google SDK import (`google.cloud.*`, `google.auth`, `googleapiclient`,
   `httplib2`, `google_auth_httplib2`) inside the handler function body

Lazy-import rule: **only Google/third-party SDK imports are lazy.** Standard library
imports are cheap and are loaded eagerly at module top. This keeps the memory
rationale in `AGENTS.md` intact.

Import notes for the larger handlers (easy to miss, only fail at runtime):
- `compute.py`: stdlib `logging`, `time`, `Callable`; lazy `google.auth`,
  `google_auth_httplib2`, `httplib2`, `compute_v1`, `googleapiclient.discovery.build`,
  `resourcemanager_v3`
- `vertexai.py`: stdlib `logging`, `Callable`; lazy `aiplatform_v1`
- `alloydb.py`, `cloudbuild.py`, `cloudbuildv2.py`, `storage.py`: stdlib `logging`, `Callable`
- `bigquery.py`: stdlib `logging`, `Callable`; also needs `underscore_prefix` from `core`
- `colab.py`: stdlib `Callable` only (no `logging`, no `time`); lazy `aiplatform_v1`
- `vertexai.py` also needs `underscore_prefix` from `core`; no other handler uses it

Type hints stay complete on every moved function (see `AGENTS.md`). The existing
`dict[str, any]` annotation in `handle_vertexai.scan_and_delete` is a pre-existing
typo (`any` instead of `typing.Any`); it is not harmful under
`from __future__ import annotations` and is intentionally left untouched in this
move-only change.

### Phase 5: Update main.py (10 minutes)
Remove the moved code and keep only the HTTP entrypoint. Keep
`from __future__ import annotations` in `main.py` (and add it to every new module).
`Request` is imported only under `TYPE_CHECKING`; Python 3.14 defers annotation
evaluation (PEP 649), so this would not fail at import today, but the future import
keeps annotations as plain strings for any tool that introspects them (e.g.
`typing.get_type_hints`) and matches the current file, so the move stays
behavior-neutral.

```python
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

import functions_framework

from janitor.config import DEFAULT_PREFIX, DEFAULT_REGION
from janitor.handlers.alloydb import handle_alloydb
from janitor.handlers.bigquery import handle_bigquery
from janitor.handlers.cloudbuild import handle_cloudbuild
from janitor.handlers.cloudbuildv2 import handle_cloudbuildv2
from janitor.handlers.colab import handle_colab
from janitor.handlers.compute import handle_compute
from janitor.handlers.storage import handle_storage
from janitor.handlers.vertexai import handle_vertexai

if TYPE_CHECKING:
    from flask import Request

# scan_and_clean_resources: body unchanged, including the order-sensitive
# `scanners` list (alloydb -> ... -> bigquery -> compute).
```

Handler modules are imported eagerly by `main.py`; that is safe because each module
defers its own Google SDK imports to function scope.

### Phase 6: Local Verification (15 minutes)
```bash
# Syntax and bytecode for every module
.venv/bin/python -m compileall -q main.py janitor

# Undefined / unused names (catches missing imports from the move)
.venv/bin/pip install ruff   # dev-only; do not add to requirements.txt
.venv/bin/ruff check --select F main.py janitor

# Entrypoint imports and no Google SDK is loaded eagerly
.venv/bin/python -c "
import sys, main
sdk = sorted(m for m in sys.modules if m.startswith(('google.cloud.compute', 'google.cloud.alloydb', 'google.cloud.aiplatform', 'google.cloud.bigquery', 'google.cloud.storage', 'google.cloud.devtools', 'google.cloud.resourcemanager', 'google.auth', 'google_auth_httplib2', 'googleapiclient', 'httplib2')))
assert not sdk, sdk
print('imports OK, lazy loading preserved')
"

# Serve the function locally and call it in dry-run mode
# (needs Application Default Credentials: `gcloud auth application-default login`)
TARGET_GCP_PROJECT_ID=<test-project> DO_CLEANUP=false \
  .venv/bin/functions-framework --target scan_and_clean_resources --port 8080 &
sleep 3
curl -s 'localhost:8080' | jq .
kill %1
```

Without `TARGET_GCP_PROJECT_ID` the function returns `400` before any handler runs,
which would hide import errors inside handlers.

`./trigger.sh` is not a local check: it invokes the *deployed* function.

### Phase 7: Behavioral Equivalence Check (15 minutes)
The refactor is move-only, so output must be identical. Run a dry-run against the same
project used for the Phase 1 baseline and compare `found_resources` (sorted) and
`found_count`. Any difference blocks the change.

### Phase 8: Deploy and Verify (20 minutes)
Before deploying, confirm the package is in the upload set:

```bash
gcloud meta list-files-for-upload | grep '^janitor/'
```

Then deploy with the existing `deploy.sh` in dry-run mode (`DO_CLEANUP` unset, so `false`),
invoke with `./trigger.sh`, and inspect `./cloudrun-logs.sh` for import errors.

`.gcloudignore` already skips everything in `.gitignore` (`.venv/`, `__pycache__/`,
`*.pyc`) plus `*.md` and `*.sh`. After the move, the full upload set should be exactly
`LICENSE`, `main.py`, `requirements.txt` and the `janitor/` package files:

```bash
gcloud meta list-files-for-upload | sort
```

Any other file in that list (especially `.venv/` content) is a regression.

### Phase 9: Documentation (10 minutes)
Update, in the same change:
- `README.md`: add "Project layout" and fix the `MAX_DELETE_WORKERS` reference (see Phase 0)
- `AGENTS.md`: file map (`main.py` row, `janitor/` row), the "`main.py` structure"
  section and the sentence saying `HandlerResult` lives at the top of `main.py`,
  the verification commands (`ast.parse('main.py')`), and the "Adding a new resource
  type" steps (new handler = new module under `janitor/handlers/` plus an import and
  a `scanners` entry in `main.py`)
- State in `AGENTS.md` that the lazy-import rule applies to Google/third-party SDKs
  only; stdlib imports may be top-level

---

## Consequences

### Positive

✅ **Testability**: Each handler can be tested in isolation by patching its own module's SDK imports.  
✅ **Maintainability**: Developers can locate domain logic without scanning 1,196 LOC.  
✅ **Parallel Development**: PRs on different handlers touch different files.  
✅ **Reusability**: Core utilities and handlers can be imported by other projects.  
✅ **Scaling**: Adding new domain handlers follows an established pattern.  
✅ **Code Review**: Handler changes isolated to single file; easier to review.  

### Negative

⚠️ **No LOC Reduction**: Refactoring adds a few dozen lines of imports and module headers.  
⚠️ **Documentation Update**: `README.md` and `AGENTS.md` must be updated in the same change (Phase 0 / Phase 9).  
⚠️ **Deletion order is spread across files**: the cross-domain order lives in `main.py`'s `scanners` list while intra-domain order lives in each handler; `AGENTS.md` must keep pointing to both.  

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Missing import after the move (only fails at runtime) | Medium | High | Phase 6 `ruff --select F` and local `functions-framework` dry-run |
| Accidental eager Google SDK import raises memory use | Low | Medium | Phase 6 `sys.modules` assertion; stdlib is the only allowed top-level import |
| Behavior change from the move | Low | Critical | Phase 7 before/after dry-run diff |
| Circular import created accidentally | Low | High | Handlers import only `core`; review enforces no handler-to-handler imports |
| `janitor/` not packaged by the deploy | Very Low | Critical | Phase 8 dry-run deploy and log inspection |

---

## Alternatives Considered

### 1. Keep Monolithic main.py
**Rejected** because:
- Continues to block parallel development and testing
- Cognitive load remains high
- Violates single responsibility principle (one large file should have one reason to change)

### 2. Extract Deletion Dependencies and Resources to a Config File
**Deferred** because:
- Service Networking, Vertex AI orchestration, and error handling logic are too complex to parametrize
- A hybrid approach (registry for simple resources, config for handler ordering) is lower risk and can be done after this modularization
- This ADR unblocks testing infrastructure needed to validate that follow-up

### 3. Migrate to the Go Runtime
**Rejected** because:
- 80–120 hours of effort for negligible cost/performance savings
- Reintroduces risk (must fully revalidate all deletion logic)
- Revisit only if team composition or requirements change

---

## Implementation Timeline

| Phase | Effort | Depends on |
|-------|--------|-----------|
| 0: README | 5 min | None |
| 1-3: Skeleton, baseline, config, core | 25 min | None |
| 4: Extract 8 handlers | 60 min | 1-3 |
| 5: Update main.py | 10 min | 4 |
| 6: Local verification | 15 min | 5 |
| 7: Equivalence check | 15 min | 6 |
| 8: Deploy and verify | 20 min | 7 |
| 9: AGENTS.md / docs | 10 min | 8 |
| **Total** | **~2h 40m (160 min)** | — |

---

## References

- **Cloud Functions Python deployment**: https://cloud.google.com/functions/docs/create-deploy-gcp-python
- **ADR template**: https://github.com/architecture-decision-record/architecture-decision-record
- **Project conventions**: See `AGENTS.md` for type hinting and lazy import requirements

---

## Approval Checklist

- [ ] Architecture reviewed by maintainers
- [ ] No conflicting changes in progress
- [ ] Test project identified for dry-run deployment
- [ ] Developer assigned to implementation
- [ ] Baseline dry-run output captured before starting

---

## Related ADRs

- **ADR 0002** (FUTURE): Config-Driven Dependency Extraction (hybrid approach: registry for simple resources, config for handler ordering; easier to validate after this change)
- **ADR 0003** (REJECTED, not written): Go Runtime Migration (not planned; Python 3.14 remains the runtime)
