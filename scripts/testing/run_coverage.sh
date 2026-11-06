#!/usr/bin/env bash
# B1.1: statement and branch coverage with terminal, HTML, XML and JSON reports.
# Usage: scripts/testing/run_coverage.sh [output-directory]

source "$(dirname "${BASH_SOURCE[0]}")/b1_common.sh"
b1_require_uv

output="${1:-$B1_EVIDENCE_ROOT/coverage}"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
b1_record_provenance "$output"
cd "$REPO_ROOT"

COVERAGE_FILE="$output/.coverage" b1_uv_run test python -m pytest tests/inference \
  -p no:cacheprovider \
  --cov --cov-config=tests/quality/coverage.ini \
  --cov-report=term-missing \
  --cov-report="html:$output/html" \
  --cov-report="xml:$output/coverage.xml" \
  --cov-report="json:$output/coverage.json" \
  --junitxml="$output/junit.xml" \
  2>&1 | tee "$output/terminal.txt"
