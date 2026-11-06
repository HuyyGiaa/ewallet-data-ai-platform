#!/usr/bin/env bash
# B1.2: Hypothesis property suites.
# Usage: scripts/testing/run_property_tests.sh [output-directory]
# Environment: HYPOTHESIS_PROFILE=b1-ci|b1-thorough, HYPOTHESIS_SEED=<integer>.

source "$(dirname "${BASH_SOURCE[0]}")/b1_common.sh"
b1_require_uv

output="${1:-$B1_EVIDENCE_ROOT/property}"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
b1_record_provenance "$output"
cd "$REPO_ROOT"

export HYPOTHESIS_PROFILE="${HYPOTHESIS_PROFILE:-b1-ci}"
seed_arguments=()
if [[ -n "${HYPOTHESIS_SEED:-}" ]]; then
  seed_arguments=("--hypothesis-seed=$HYPOTHESIS_SEED")
fi

b1_uv_run test python -m pytest tests/property \
  -p no:cacheprovider \
  --hypothesis-show-statistics \
  --junitxml="$output/junit-$HYPOTHESIS_PROFILE.xml" \
  "${seed_arguments[@]}" \
  2>&1 | tee "$output/terminal-$HYPOTHESIS_PROFILE.txt"
