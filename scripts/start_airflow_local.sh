#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPOSITORY_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
export AIRFLOW_HOME="${REPOSITORY_ROOT}/data_platform/orchestration/airflow"
export AIRFLOW__CORE__DAGS_FOLDER="${AIRFLOW_HOME}/dags"
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:8084/execution/"
readonly HEALTH_URL="http://127.0.0.1:8084/api/v2/monitor/health"
readonly RUNTIME_DIR="${AIRFLOW_HOME}/logs/local-runtime"

usage() {
  cat <<'EOF'
Usage: ./scripts/start_airflow_local.sh [--status]

With no option, reuse a healthy local Airflow 3 runtime or start its API
server, scheduler, and DAG processor. --status is read-only. This helper never
initializes or resets the metadata database.
EOF
}

resolve_airflow() {
  if [[ -n ${AIRFLOW_BIN:-} && -x ${AIRFLOW_BIN} ]]; then
    return
  fi
  if [[ -n ${CONDA_PREFIX:-} && ${CONDA_DEFAULT_ENV:-} == airflow \
        && -x ${CONDA_PREFIX}/bin/airflow ]]; then
    AIRFLOW_BIN="${CONDA_PREFIX}/bin/airflow"
  elif [[ -x ${HOME}/miniconda3/envs/airflow/bin/airflow ]]; then
    AIRFLOW_BIN="${HOME}/miniconda3/envs/airflow/bin/airflow"
  else
    echo "ERROR: Airflow 3 CLI not found. Activate conda env 'airflow' or set AIRFLOW_BIN." >&2
    exit 1
  fi
  export AIRFLOW_BIN
}

component_pids() {
  local component=$1 env_file pid cmd
  for env_file in /proc/[0-9]*/environ; do
    pid=${env_file#/proc/}
    pid=${pid%/environ}
    cmd=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)
    case "${component}" in
      api-server)
        # Airflow 3's daemonized API worker may not retain AIRFLOW_HOME in
        # /proc/<pid>/environ. Bind it to this runtime by its dedicated listen
        # address instead; this also excludes any legacy API server on 8080.
        [[ ${cmd} == *"airflow api-server"* || ${cmd} == *"airflow api_server"* ]] || continue
        [[ ${cmd} == *"127.0.0.1"* && ${cmd} == *"8084"* ]] && echo "${pid}"
        ;;
      scheduler)
        grep -zqx "AIRFLOW_HOME=${AIRFLOW_HOME}" "${env_file}" 2>/dev/null || continue
        [[ ${cmd} == *"/airflow scheduler"* ]] && echo "${pid}"
        ;;
      dag-processor)
        grep -zqx "AIRFLOW_HOME=${AIRFLOW_HOME}" "${env_file}" 2>/dev/null || continue
        [[ ${cmd} == *"/airflow dag-processor"* ]] && echo "${pid}"
        ;;
    esac
  done
}

runtime_status() {
  local failed=0 component pids health
  for component in api-server scheduler dag-processor; do
    mapfile -t pids < <(component_pids "${component}")
    if (( ${#pids[@]} == 1 )); then
      printf '%-14s RUNNING pid=%s\n' "${component}" "${pids[0]}"
    elif (( ${#pids[@]} == 0 )); then
      printf '%-14s STOPPED\n' "${component}"
      failed=1
    else
      printf '%-14s CONFLICT pids=%s\n' "${component}" "${pids[*]}"
      failed=1
    fi
  done

  health=$(curl --connect-timeout 2 --max-time 5 --fail --silent "${HEALTH_URL}" 2>/dev/null || true)
  if [[ ${health} == *'"metadatabase":{"status":"healthy"}'* \
        && ${health} == *'"scheduler":{"status":"healthy"'* \
        && ${health} == *'"dag_processor":{"status":"healthy"'* ]]; then
    echo "health         HEALTHY ${HEALTH_URL}"
  else
    echo "health         NOT_READY ${HEALTH_URL}"
    failed=1
  fi
  return "${failed}"
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --status)
    resolve_airflow
    "${AIRFLOW_BIN}" version >/dev/null
    runtime_status
    exit $?
    ;;
  "") ;;
  *)
    usage >&2
    exit 2
    ;;
esac

resolve_airflow
version=$("${AIRFLOW_BIN}" version)
if [[ ! ${version} =~ ^3\. ]]; then
  echo "ERROR: expected Airflow 3.x, found ${version}." >&2
  exit 1
fi

if runtime_status; then
  echo "Airflow local runtime is already healthy; reusing it."
  exit 0
fi

mapfile -t existing < <(
  component_pids api-server
  component_pids scheduler
  component_pids dag-processor
)
if (( ${#existing[@]} > 0 )) || ss -ltn 2>/dev/null | grep -qE ':[[:space:]]*8084\b|:8084\b'; then
  echo "ERROR: partial/conflicting Airflow runtime detected; refusing to start duplicates." >&2
  echo "Resolve the processes shown by --status, then retry. No process was killed." >&2
  exit 1
fi

# Read-only readiness check: never create, migrate, or reset metadata state here.
"${AIRFLOW_BIN}" db check
mkdir -p "${RUNTIME_DIR}"

"${AIRFLOW_BIN}" api-server --daemon --host 127.0.0.1 --port 8084 \
  --pid "${RUNTIME_DIR}/api-server.pid" \
  --stdout "${RUNTIME_DIR}/api-server.out" \
  --stderr "${RUNTIME_DIR}/api-server.err"
"${AIRFLOW_BIN}" scheduler --daemon \
  --pid "${RUNTIME_DIR}/scheduler.pid" \
  --stdout "${RUNTIME_DIR}/scheduler.out" \
  --stderr "${RUNTIME_DIR}/scheduler.err"
"${AIRFLOW_BIN}" dag-processor --daemon \
  --pid "${RUNTIME_DIR}/dag-processor.pid" \
  --stdout "${RUNTIME_DIR}/dag-processor.out" \
  --stderr "${RUNTIME_DIR}/dag-processor.err"

for _ in $(seq 1 30); do
  if runtime_status >/dev/null 2>&1; then
    runtime_status
    echo "Airflow UI/API: http://127.0.0.1:8084"
    exit 0
  fi
  sleep 2
done

runtime_status || true
echo "ERROR: Airflow did not become healthy within 60 seconds. Inspect ${RUNTIME_DIR}." >&2
exit 1
