#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPOSITORY_ROOT"

AUDIT_PYTHON="${EWALLET_AUDIT_PYTHON:-python3}"
exec "$AUDIT_PYTHON" -m ops.rubric.validate_evidence_matrix \
  --matrix docs/rubric/rubric_evidence_matrix.csv
