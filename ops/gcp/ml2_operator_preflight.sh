#!/usr/bin/env bash
set -euo pipefail

: "${REPO_ROOT:?Set REPO_ROOT to the isolated ML-1 worktree}"
: "${EXPECTED_REVIEWED_GIT_SHA:?Supply the externally reviewed 40-character SHA}"
: "${DVC_BIN:?Supply the dedicated DVC 3.63.0 executable}"
: "${FEATURE_SELECTION_EVIDENCE:?Missing feature-selection evidence}"
: "${PIT_EVIDENCE:?Missing PIT evidence}"
: "${REGISTRY_READBACK_EVIDENCE:?Missing Registry readback evidence}"
: "${TERRAFORM_OUTPUT_JSON:?Missing vetted Terraform output evidence}"

python -m ops.gcp.ml2_operator_guard preflight \
  --repo "$REPO_ROOT" \
  --expected-reviewed-git-sha "$EXPECTED_REVIEWED_GIT_SHA" \
  --expected-branch codex/ml1-training-snapshot \
  --dvc-executable "$DVC_BIN" \
  --evidence "$FEATURE_SELECTION_EVIDENCE" \
  --evidence "$PIT_EVIDENCE" \
  --evidence "$REGISTRY_READBACK_EVIDENCE" \
  --evidence "$TERRAFORM_OUTPUT_JSON"

printf 'PASS | source-only operator preflight; no cloud success is implied\n'
