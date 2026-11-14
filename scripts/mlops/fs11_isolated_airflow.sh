#!/usr/bin/env bash
# Run an isolated Airflow 3 control plane that sees only the Feature Store DAG.
#
#   scripts/mlops/fs11_isolated_airflow.sh start    # init its own DB, start 3 components
#   scripts/mlops/fs11_isolated_airflow.sh status
#   scripts/mlops/fs11_isolated_airflow.sh airflow <args...>   # CLI against this instance
#   scripts/mlops/fs11_isolated_airflow.sh stop     # SIGTERM to this instance's PIDs only
#
# AIRFLOW_HOME ~/.cache/ewallet-fs11-airflow, UI 127.0.0.1:8094, log servers 8796/8797,
# DAG feast_incremental_materialization only. The mechanics are in scripts/isolated_airflow.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ISOLATED_AIRFLOW_HOME="${FS11_AIRFLOW_HOME:-$HOME/.cache/ewallet-fs11-airflow}"
export ISOLATED_AIRFLOW_PORT="${FS11_AIRFLOW_PORT:-8094}"
export ISOLATED_AIRFLOW_WORKER_LOG_PORT="${FS11_WORKER_LOG_PORT:-8796}"
export ISOLATED_AIRFLOW_TRIGGER_LOG_PORT="${FS11_TRIGGER_LOG_PORT:-8797}"
export ISOLATED_AIRFLOW_DAGS="data_platform/orchestration/airflow/dags/feast_incremental_materialization.py"
# The DataHub task resolves uv from this variable, then checks the DataHub lock.
if [[ -z "${EWALLET_DATAHUB_UV:-}" ]]; then
  EWALLET_DATAHUB_UV="$(command -v uv || true)"
  [[ -n "$EWALLET_DATAHUB_UV" || ! -x "$HOME/miniconda3/envs/fintech/bin/uv" ]] \
    || EWALLET_DATAHUB_UV="$HOME/miniconda3/envs/fintech/bin/uv"
fi
export EWALLET_DATAHUB_UV
# Run artifacts land in $EWALLET_C1_EVIDENCE_ROOT/<run_id>/ (rendered at DAG parse).
export EWALLET_C1_EVIDENCE_ROOT="${EWALLET_C1_EVIDENCE_ROOT:-$HOME/ewallet-evidence/c1-feast-incremental}"
exec "$ROOT/scripts/isolated_airflow.sh" "$@"
