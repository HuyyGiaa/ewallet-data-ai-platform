#!/usr/bin/env bash
# Check, and only on explicit approval trigger, one DP2 DagRun (dp2_bronze_to_curated).
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

usage() {
  cat <<'USAGE'
Usage: bash scripts/edai1/run_dp2.sh --benchmark-id ID --profile baseline|optimized
                                     [--offline] [--execute --confirm-paid]

DP2: DP1 Bronze -> Silver -> Core Gold, publishes dp2_handoff.json.
DAG: dp2_bronze_to_curated. Refuses unless the DP1 handoff of the same benchmark and profile verifies.

Default is a DRY RUN: it prints the parameters, the Spark profile and the approval
token, and checks Git (main, clean, the benchmark's pinned SHA), the DAG, that the
DagRun ID is unused, and (read-only GCS/Dataproc) the upstream handoff and that the
output prefix, batch ID and handoff of this run are absent. Exit 0 = ready.

  --offline        dry run without the read-only GCS/Dataproc checks
  --execute        trigger this one DagRun in the local Airflow (127.0.0.1:8084)
  --confirm-paid   required with --execute: the DagRun submits paid Dataproc batches

It never triggers any other DAG. Parameters come from
tmp/edai1-campaign/<benchmark>/<profile>/dp2.json (scripts/edai1/prepare_campaign.sh).
Exit codes: 0 ready/triggered, 1 refused or not ready, 2 usage error.
USAGE
}

if edai1_help_requested "$@"; then usage; exit 0; fi
edai1_cli run --pipeline dp2 "$@"
