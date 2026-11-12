#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Locked environment (deltalake, pyarrow) kept outside the checkout.
UV="${UV:-uv}"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.cache/ewallet-venvs/feast}"
exec "$UV" run --locked --project ai_platform/features/feast python -m ops.gcp.inspect_storage_layout "$@"
