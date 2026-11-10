#!/usr/bin/env bash
# Read-only verification of DP1 -> DP2 -> DP3 handoffs for one profile.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

usage() {
  cat <<'USAGE'
Usage: bash scripts/edai1/verify_handoffs.sh --benchmark-id ID --profile baseline|optimized

For dp1_handoff.json, dp2_handoff.json and dp3_completion.json of the profile: the
manifest URI, whether it exists, its dataset, Source SHA, Git SHA, profile, run IDs,
producing DagRun and validation verdicts, re-checked against the validation reports,
and whether the next DAG may start. An absent manifest means "not ready yet".

Read-only; no Dataproc jobs. Exit codes: 0 every published handoff verified,
1 a published handoff failed verification or could not be read, 2 usage error.
USAGE
}

if edai1_help_requested "$@"; then usage; exit 0; fi
edai1_cli verify-handoffs "$@"
