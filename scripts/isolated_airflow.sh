#!/usr/bin/env bash
# Run an isolated Airflow 3 control plane that sees only the DAG files it is given.
#
#   scripts/isolated_airflow.sh start              init its own DB, start api-server, scheduler, dag-processor
#   scripts/isolated_airflow.sh status
#   scripts/isolated_airflow.sh airflow <args...>  CLI against this instance
#   scripts/isolated_airflow.sh stop               SIGTERM to this instance's PIDs only
#
# Callers set (see scripts/mlops/fs11_isolated_airflow.sh, scripts/llm_agent/rag_isolated_airflow.sh):
#   ISOLATED_AIRFLOW_HOME              its own AIRFLOW_HOME and SQLite metadata DB
#   ISOLATED_AIRFLOW_DAGS              repository-relative DAG files, space separated; the DAG folder
#                                      holds one symlink per file and nothing else
#   ISOLATED_AIRFLOW_PORT              API server and UI on 127.0.0.1
#   ISOLATED_AIRFLOW_WORKER_LOG_PORT, ISOLATED_AIRFLOW_TRIGGER_LOG_PORT
# The laptop's other Airflow instances, their homes and their DAGs are never read or touched.
# Processes are tracked by PID files and stopped only after their command line is checked.
# Nothing is installed: the Airflow interpreter is reused read-only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AIRFLOW_BIN="${AIRFLOW_BIN:-$HOME/miniconda3/envs/airflow/bin/airflow}"
: "${ISOLATED_AIRFLOW_HOME:?set ISOLATED_AIRFLOW_HOME}" "${ISOLATED_AIRFLOW_DAGS:?set ISOLATED_AIRFLOW_DAGS}"
: "${ISOLATED_AIRFLOW_PORT:?set ISOLATED_AIRFLOW_PORT}"
: "${ISOLATED_AIRFLOW_WORKER_LOG_PORT:?}" "${ISOLATED_AIRFLOW_TRIGGER_LOG_PORT:?}"
export AIRFLOW_HOME="$ISOLATED_AIRFLOW_HOME"
PORT="$ISOLATED_AIRFLOW_PORT"
read -r -a DAG_FILES <<<"$ISOLATED_AIRFLOW_DAGS"

export AIRFLOW__CORE__DAGS_FOLDER="$AIRFLOW_HOME/dags"
export AIRFLOW__CORE__EXECUTOR=LocalExecutor
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__PARALLELISM=4
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///$AIRFLOW_HOME/airflow.db"
export AIRFLOW__API__HOST=127.0.0.1
export AIRFLOW__API__PORT="$PORT"
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:$PORT/execution/"
export AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT="$ISOLATED_AIRFLOW_WORKER_LOG_PORT"
export AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT="$ISOLATED_AIRFLOW_TRIGGER_LOG_PORT"
export AIRFLOW__LOGGING__BASE_LOG_FOLDER="$AIRFLOW_HOME/logs"
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=True
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=30
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
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

link_dags() {
  local file allowed=()
  mkdir -p "$AIRFLOW_HOME/dags" "$AIRFLOW_HOME/logs"
  for file in "${DAG_FILES[@]}"; do
    [[ -f "$ROOT/$file" ]] || die "DAG file not found: $file"
    ln -sfn "$ROOT/$file" "$AIRFLOW_HOME/dags/$(basename "$file")"
    allowed+=(! -name "$(basename "$file")")
  done
  find "$AIRFLOW_HOME/dags" -mindepth 1 "${allowed[@]}" ! -name __pycache__ ! -path '*/__pycache__/*' \
    -print -quit | grep -q . && die "$AIRFLOW_HOME/dags must contain only: ${DAG_FILES[*]}"
  return 0
}

cmd_start() {
  [[ -x "$AIRFLOW_BIN" ]] || die "Airflow CLI not found: $AIRFLOW_BIN"
  [[ "$("$AIRFLOW_BIN" version)" == 3.* ]] || die "expected Airflow 3.x"
  [[ "$AIRFLOW_HOME" != "$ROOT/data_platform/orchestration/airflow" && "$AIRFLOW_HOME" != "$HOME/airflow" ]] \
    || die "refusing a shared AIRFLOW_HOME"
  link_dags
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
  *) sed -n '2,7p' "$0" >&2; exit 2 ;;
esac
