#!/usr/bin/env bash
# Shared settings for the B1 testing runners. Source this file; do not run it.

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
B1_PROJECT="$REPO_ROOT/ai_platform/inference"
B1_EVIDENCE_ROOT="${B1_EVIDENCE_ROOT:-$REPO_ROOT/docs/evidence/edai2/testing/b1}"
export PYTHONDONTWRITEBYTECODE=1

b1_require_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not on PATH. Example: export PATH=\"\$HOME/miniconda3/envs/fintech/bin:\$PATH\"" >&2
    exit 127
  fi
}

# Run a command in the locked Fraud API environment with the given group.
b1_uv_run() {
  local group="$1"
  shift
  uv run --locked --project "$B1_PROJECT" --group "$group" "$@"
}

b1_record_provenance() {
  local directory="$1"
  mkdir -p "$directory"
  {
    echo "git_commit=$(git -C "$REPO_ROOT" rev-parse HEAD)"
    echo "git_branch=$(git -C "$REPO_ROOT" branch --show-current)"
    echo "generated_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "uv_lock_sha256=$(sha256sum "$B1_PROJECT/uv.lock" | cut -d' ' -f1)"
  } >"$directory/provenance.txt"
}
