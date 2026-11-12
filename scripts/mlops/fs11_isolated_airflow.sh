#!/usr/bin/env bash
# Run an isolated Airflow 3 control plane that sees only the Row 11 DAG.
#
#   scripts/mlops/fs11_isolated_airflow.sh start    # init its own DB, start 3 components
#   scripts/mlops/fs11_isolated_airflow.sh status
#   scripts/mlops/fs11_isolated_airflow.sh airflow <args...>   # CLI against this instance
#   scripts/mlops/fs11_isolated_airflow.sh stop     # SIGTERM to this instance's PIDs only
#
# Isolation from the laptop's existing Airflow (port 8084, its own AIRFLOW_HOME):
# * AIRFLOW_HOME defaults to ~/.cache/ewallet-fs11-airflow with its own SQLite DB;
# * the DAG folder holds one symlink, to feast_incremental_materialization.py, so no
#   DP1-DP3 DAG is parsed or scheduled here;
# * API server 127.0.0.1:8094, worker log server 8796, triggerer log server 8797;
# * processes are tracked by PID files in AIRFLOW_HOME and stopped only by those
#   PIDs after their command line is checked. Nothing is installed: the Airflow
#   interpreter is reused read-only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AIRFLOW_BIN="${AIRFLOW_BIN:-$HOME/miniconda3/envs/airflow/bin/airflow}"
export AIRFLOW_HOME="${FS11_AIRFLOW_HOME:-$HOME/.cache/ewallet-fs11-airflow}"
PORT="${FS11_AIRFLOW_PORT:-8094}"
DAG_FILE="$ROOT/data_platform/orchestration/airflow/dags/feast_incremental_materialization.py"

export AIRFLOW__CORE__DAGS_FOLDER="$AIRFLOW_HOME/dags"
export AIRFLOW__CORE__EXECUTOR=LocalExecutor
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__PARALLELISM=4
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///$AIRFLOW_HOME/airflow.db"
export AIRFLOW__API__HOST=127.0.0.1
export AIRFLOW__API__PORT="$PORT"
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:$PORT/execution/"
export AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT="${FS11_WORKER_LOG_PORT:-8796}"
export AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT="${FS11_TRIGGER_LOG_PORT:-8797}"
export AIRFLOW__LOGGING__BASE_LOG_FOLDER="$AIRFLOW_HOME/logs"
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=True
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=30
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
# The DataHub task resolves uv from this variable, then checks the DataHub lock.
if [[ -z "${EWALLET_DATAHUB_UV:-}" ]]; then
  EWALLET_DATAHUB_UV="$(command -v uv || true)"
  [[ -n "$EWALLET_DATAHUB_UV" || ! -x "$HOME/miniconda3/envs/fintech/bin/uv" ]] \
    || EWALLET_DATAHUB_UV="$HOME/miniconda3/envs/fintech/bin/uv"
fi
export EWALLET_DATAHUB_UV
# Run artifacts land in $EWALLET_C1_EVIDENCE_ROOT/<run_id>/ (rendered at DAG parse).
export EWALLET_C1_EVIDENCE_ROOT="${EWALLET_C1_EVIDENCE_ROOT:-$HOME/ewallet-evidence/c1-feast-incremental}"
COMPONENTS=(api-server scheduler dag-processor)

die() { echo "ERROR: $*" >&2; exit 1; }

pid_alive() {
  local name="$1" pid
  [[ -f "$AIRFLOW_HOME/$name.pid" ]] || return 1
  pid=$(<"$AIRFLOW_HOME/$name.pid")
  # Airflow retitles some processes, e.g. "airflow api_server -- host:...".
  [[ -r "/proc/$pid/cmdline" ]] && tr '\0' ' ' <"/proc/$pid/cmdline" \
    | grep -Eq "airflow (${name}|${name//-/_})( |$)"
}

cmd_start() {
  [[ -x "$AIRFLOW_BIN" ]] || die "Airflow CLI not found: $AIRFLOW_BIN"
  [[ "$("$AIRFLOW_BIN" version)" == 3.* ]] || die "expected Airflow 3.x"
  [[ "$AIRFLOW_HOME" != "$ROOT/data_platform/orchestration/airflow" ]] || die "refusing the shared AIRFLOW_HOME"
  mkdir -p "$AIRFLOW_HOME/dags" "$AIRFLOW_HOME/logs"
  ln -sfn "$DAG_FILE" "$AIRFLOW_HOME/dags/feast_incremental_materialization.py"
  find "$AIRFLOW_HOME/dags" -mindepth 1 ! -name feast_incremental_materialization.py \
    ! -name __pycache__ ! -path '*/__pycache__/*' -print -quit \
    | grep -q . && die "$AIRFLOW_HOME/dags must contain only the Row 11 DAG"
  "$AIRFLOW_BIN" db migrate >"$AIRFLOW_HOME/logs/db-migrate.log" 2>&1
  for name in "${COMPONENTS[@]}"; do
    if pid_alive "$name"; then echo "$name already running"; continue; fi
    if [[ "$name" == api-server ]] && ss -ltn | grep -q "127.0.0.1:$PORT "; then
      die "port $PORT is in use by another process"
    fi
    nohup "$AIRFLOW_BIN" "$name" >"$AIRFLOW_HOME/logs/$name.out" 2>&1 &
    echo $! >"$AIRFLOW_HOME/$name.pid"
    echo "started $name pid $!"
  done
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$PORT/api/v2/monitor/health" >/dev/null 2>&1 && break
    sleep 2
  done
  cmd_status
}

cmd_status() {
  local name
  echo "AIRFLOW_HOME=$AIRFLOW_HOME  UI=http://127.0.0.1:$PORT"
  for name in "${COMPONENTS[@]}"; do
    if pid_alive "$name"; then echo "$name: running pid $(<"$AIRFLOW_HOME/$name.pid")"; else echo "$name: stopped"; fi
  done
  curl -fsS "http://127.0.0.1:$PORT/api/v2/monitor/health" 2>/dev/null || echo "health: unreachable"
  echo
}

cmd_stop() {
  local name pid
  for name in "${COMPONENTS[@]}"; do
    if pid_alive "$name"; then
      pid=$(<"$AIRFLOW_HOME/$name.pid")
      kill -TERM "$pid" && echo "sent SIGTERM to $name pid $pid"
    fi
    rm -f "$AIRFLOW_HOME/$name.pid"
  done
}

case "${1:-}" in
  start) cmd_start ;;
  status) cmd_status ;;
  stop) cmd_stop ;;
  airflow) shift; exec "$AIRFLOW_BIN" "$@" ;;
  *) sed -n '2,8p' "$0" >&2; exit 2 ;;
esac
