# Standard configuration defaults
DEFAULT_REGION = "us-central1"
DEFAULT_PREFIX = "ansible-test-"

# Bounded worker count for concurrent delete operations. Deletes are
# I/O-bound (almost all wall time is spent blocked on GCP long-running
# operations), so this is intentionally NOT derived from CPU core count —
# doing so would collapse concurrency back to ~1 on a single-vCPU Cloud Run
# instance. The value is a fixed middle ground: comfortably more than a
# typical leftover-resource batch, well under GCP's per-project concurrent
# operation limits.
MAX_DELETE_WORKERS = 8
