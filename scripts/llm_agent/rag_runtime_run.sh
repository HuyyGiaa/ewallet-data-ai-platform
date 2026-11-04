#!/usr/bin/env bash
# Canonical RAG runtime run on the EXISTING Airflow (rubric edai-2 R9/R10).
#
#   rag_runtime_run.sh preflight            read-only checks, prints PASS/FAIL per item
#   rag_runtime_run.sh trigger RUN_LABEL    unpause + trigger rag_fraud_policy (redis, publish), wait, collect
#   rag_runtime_run.sh collect RUN_LABEL    copy Airflow states/logs and DataHub/Redis readback into the run dir
#
# Configuration (names only; values live in ~/.config/ewallet-rag/runtime.env, see
# infra/docker/rag-ingestion/runtime.env.example):
#   AIRFLOW_HOME / AIRFLOW_BIN     the live Airflow 3 installation (default ~/airflow, conda env "airflow")
#   RAG_KUBE_CONTEXT               if set, a kubectl port-forward to feature-store/svc/redis runs on
#                                  127.0.0.1:${RAG_REDIS_LOCAL_PORT:-16380} for the duration of the run
# Evidence: docs/evidence/llm_agent/rag_<RUN_LABEL>/ (written by the DAG tasks and by collect).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AIRFLOW_HOME="${AIRFLOW_HOME:-${HOME}/airflow}"
AIRFLOW_BIN="${AIRFLOW_BIN:-${HOME}/miniconda3/envs/airflow/bin/airflow}"
ENV_FILE="${EWALLET_RAG_ENV_FILE:-${HOME}/.config/ewallet-rag/runtime.env}"
RAG_PY="${ROOT}/infra/docker/rag-ingestion/.venv/bin/python"
DAG_ID=rag_fraud_policy
TASKS=(discover_documents parse_and_chunk generate_embeddings publish_feast_features validate_online_readback publish_datahub_lineage)
FORWARD_PID=""

af() { "${AIRFLOW_BIN}" "$@" 2>/dev/null; }
env_value() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1; }
# Captured first: grep -q closing a pipe early would fail the pipeline under pipefail.
dag_line() { local listing; listing=$(af dags list -o plain); grep "^${DAG_ID} " <<<"${listing}"; }
no_import_error() { local errors; errors=$(af dags list-import-errors -o plain); ! grep -q "${DAG_ID}" <<<"${errors}"; }
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; FAILED=1; fi; }

start_forward() {
  [[ -n "${RAG_KUBE_CONTEXT:-}" ]] || return 0
  kubectl --context "${RAG_KUBE_CONTEXT}" -n feature-store port-forward svc/redis \
    "${RAG_REDIS_LOCAL_PORT:-16380}:6379" >/dev/null 2>&1 &
  FORWARD_PID=$!
  trap '[[ -n "${FORWARD_PID}" ]] && kill "${FORWARD_PID}" 2>/dev/null || true' EXIT
  sleep 3
}

redis_ping() {
  "${RAG_PY}" - "$(env_value EWALLET_REDIS_CONNECTION_STRING)" <<'EOF'
import sys, redis
host_port, *options = sys.argv[1].split(",")
host, port = host_port.rsplit(":", 1)
kwargs = dict(item.split("=", 1) for item in options)
redis.Redis(host=host, port=int(port), password=kwargs.get("password"), ssl=kwargs.get("ssl") == "true",
            socket_timeout=5).ping()
EOF
}

preflight() {
  FAILED=0
  cd "${ROOT}"
  check "DAG source has the six ingestion tasks" "grep -q '\"publish_datahub_lineage\", \"datahub\"' data_platform/orchestration/airflow/dags/${DAG_ID}.py"
  check "uv lock consistent (infra/docker/rag-ingestion)" "${UV:-uv} lock --check --project infra/docker/rag-ingestion"
  check "locked task runtime present (${RAG_PY})" "\"${RAG_PY}\" -c 'import feast, onnxruntime, datahub'"
  check "runtime env file exists and is private (${ENV_FILE})" "test -f '${ENV_FILE}' && [ \"\$(stat -c %a '${ENV_FILE}')\" = 600 ]"
  for key in EWALLET_RAG_FEAST_REGISTRY EWALLET_REDIS_CONNECTION_STRING EWALLET_RAG_PUBLICATION_BASE DATAHUB_GMS_URL; do
    check "runtime env sets ${key}" "test -n \"\$(env_value ${key})\""
  done
  check "live Airflow parses ${DAG_ID} without import errors" "dag_line && no_import_error"
  check "Airflow DAGs folder is this checkout" "dag_line | grep -q '${ROOT}/'"
  start_forward
  check "Feast Online Redis answers PING" "redis_ping"
  check "DataHub GMS healthy" "curl -fsS --max-time 10 \"\$(env_value DATAHUB_GMS_URL)/health\""
  check "GCS publication base readable" "gcloud storage ls \"\$(env_value EWALLET_RAG_PUBLICATION_BASE | sed 's#/*\$##' | xargs dirname)\""
  return "${FAILED}"
}

collect() {
  local label=$1 run_id dir
  dir="${ROOT}/docs/evidence/llm_agent/rag_${label}"
  mkdir -p "${dir}/airflow"
  run_id=$(cat "${dir}/airflow/run_id.txt")
  git -C "${ROOT}" rev-parse HEAD > "${dir}/airflow/git_sha.txt"
  af dags list-runs "${DAG_ID}" -o json > "${dir}/airflow/dag_runs.json" || true
  af tasks states-for-dag-run "${DAG_ID}" "${run_id}" -o json > "${dir}/airflow/task_states.json"
  for task in "${TASKS[@]}"; do
    find "${AIRFLOW_HOME}/logs/dag_id=${DAG_ID}/run_id=${run_id}/task_id=${task}" -name '*.log' \
      -exec sh -c 'for f; do echo "### $f"; cat "$f"; done' _ {} + > "${dir}/airflow/${task}.log" 2>/dev/null || true
  done
  # Raw DataHub readback of the lineage edge and the flow (independent of the task's own readback).
  if [[ -f "${dir}/datahub.json" ]]; then
    "${RAG_PY}" - "${dir}" "$(env_value DATAHUB_GMS_URL)" <<'EOF'
import json, sys, urllib.parse, urllib.request
directory, gms = sys.argv[1], sys.argv[2].rstrip("/")
summary = json.load(open(f"{directory}/datahub.json"))
urns = {summary["feast_dataset"]: "upstreamLineage",
        "urn:li:dataFlow:(airflow,rag_fraud_policy,ewallet-gcp)": "dataFlowInfo"}
out = {}
for urn, aspect in urns.items():
    url = f"{gms}/aspects/{urllib.parse.quote(urn, safe='')}?aspect={aspect}&version=0"
    with urllib.request.urlopen(url, timeout=15) as response:
        out[f"{urn}#{aspect}"] = json.load(response)
json.dump(out, open(f"{directory}/datahub_raw_readback.json", "w"), indent=2)
EOF
  fi
  echo "collected into ${dir}"
}

trigger() {
  local label=$1 run_id dir state
  [[ "${label}" =~ ^[a-z0-9][a-z0-9_-]{2,63}$ ]] || { echo "invalid RUN_LABEL" >&2; exit 2; }
  dir="${ROOT}/docs/evidence/llm_agent/rag_${label}"
  [[ ! -e "${dir}" ]] || { echo "evidence dir exists: ${dir}" >&2; exit 2; }
  preflight
  run_id="manual__rag-${label}"
  af dags unpause "${DAG_ID}" >/dev/null
  af dags trigger "${DAG_ID}" --run-id "${run_id}" \
    --conf "{\"online_store\": \"redis\", \"datahub_mode\": \"publish\", \"run_label\": \"${label}\"}" >/dev/null
  mkdir -p "${dir}/airflow" && echo "${run_id}" > "${dir}/airflow/run_id.txt"
  for _ in $(seq 1 180); do
    state=$(af dags list-runs "${DAG_ID}" -o json | "${RAG_PY}" -c \
      "import json,sys;print(next((r['state'] for r in json.load(sys.stdin) if r['run_id']=='${run_id}'),'unknown'))")
    echo "$(date -u +%FT%TZ) ${run_id} ${state}"
    [[ "${state}" == success || "${state}" == failed ]] && break
    sleep 20
  done
  collect "${label}"
  [[ "${state}" == success ]]
}

case "${1:-}" in
  preflight) preflight ;;
  trigger) trigger "${2:?RUN_LABEL}" ;;
  collect) collect "${2:?RUN_LABEL}" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
