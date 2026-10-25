#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Dry-run is the default.  Cloud mutation is reachable only when the caller
# explicitly forwards --execute.
python -m ops.gcp.run_storage_benchmark "$@"
