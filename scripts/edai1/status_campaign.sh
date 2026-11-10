#!/usr/bin/env bash
# Read-only status of the six DagRuns of one EDAI-1 benchmark.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

usage() {
  cat <<'USAGE'
Usage: bash scripts/edai1/status_campaign.sh --benchmark-id ID [--dataproc] [--offline]

Prints PROFILE, DAG, Airflow DagRun STATUS (NOT_STARTED when the planned DagRun does
not exist, UNKNOWN when Airflow cannot be read), HANDOFF (PUBLISHED, ABSENT, UNKNOWN),
the DagRun ID and the current or last task, then the Airflow links and, from each
published handoff, the Dataproc batch IDs, Spark application IDs, Spark UI and event
log locations and validation status.

  --dataproc   also describe the Dataproc batches of runs without a handoff (read-only)
  --offline    read Airflow only; do not read GCS

Read-only. Exit codes: 0 printed, 1 refused, 2 usage error.
USAGE
}

if edai1_help_requested "$@"; then usage; exit 0; fi
edai1_cli status "$@"
