#!/usr/bin/env bash
# Plan one EDAI-1 Baseline/Optimized benchmark: six DagRun configurations.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

usage() {
  cat <<'USAGE'
Usage: bash scripts/edai1/prepare_campaign.sh [--dataset-version rehearsal-5gb-v1]
          [--terraform-output-json /abs/outputs.json] [--source-manifest-sha256 SHA]
          [--benchmark-id ab-YYYYMMDDhhmmss] [--skip-source-check]

Requires branch main and a clean working tree. Pins the current Git SHA, the Source
manifest SHA-256 (default for rehearsal-5gb-v1: the published ac2dfb0e…) and the
Terraform deployment (copied into the campaign directory), generates a new benchmark
ID and distinct run IDs per stage and profile, and writes

  tmp/edai1-campaign/<benchmark_id>/campaign.json
  tmp/edai1-campaign/<benchmark_id>/{baseline,optimized}/{dp1,dp2,dp3}.json

(ignored by Git, mode 600, approval set to NOT_APPROVED). It verifies the Source
read-only on GCS unless --skip-source-check. It never regenerates the Source, never
triggers a DAG and never submits Dataproc work. EDAI1_CAMPAIGN_DIR overrides the
directory. Without --terraform-output-json it runs `terraform output -json`.
Exit codes: 0 prepared, 1 refused, 2 usage error.
USAGE
}

if edai1_help_requested "$@"; then usage; exit 0; fi
edai1_cli prepare "$@"
