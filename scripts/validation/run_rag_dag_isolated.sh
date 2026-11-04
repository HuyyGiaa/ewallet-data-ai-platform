#!/usr/bin/env bash
# Execute every rag_fraud_policy_ingestion task with a real Airflow interpreter
# (`airflow dags test`) in a throwaway AIRFLOW_HOME and metadata DB.
# Never reads the live Airflow config/DB and never touches a cluster: Feast
# uses the explicit sqlite local-dev profile. DataHub runs plan_only unless the
# optional third argument is "publish" and DATAHUB_GMS_URL is set, which
# publishes to and reads back from that DataHub.
# RAG_ISOLATED_ONLINE_STORE=redis instead uses the operator's private env file
# (EWALLET_RAG_ENV_FILE, default ~/.config/ewallet-rag/runtime.env): the real
# Feast Online Redis, RAG registry and GCS publication base.
# The live Airflow metadata DB/UI is never used, so this is not the canonical
# Airflow run even with real backends.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AIRFLOW_BIN="${1:?usage: $0 /path/to/airflow-env/bin/airflow RUN_LABEL [plan_only|publish]}"
RUN_LABEL="${2:?usage: $0 /path/to/airflow-env/bin/airflow RUN_LABEL [plan_only|publish]}"
DATAHUB_MODE="${3:-plan_only}"
ONLINE_STORE="${RAG_ISOLATED_ONLINE_STORE:-sqlite}"
DAG_ID="rag_fraud_policy_ingestion"
RAG_PY="${ROOT_DIR}/infra/docker/rag-ingestion/.venv/bin/python"
OPERATOR_ENV_FILE="${EWALLET_RAG_ENV_FILE:-${HOME}/.config/ewallet-rag/runtime.env}"
[[ "${ONLINE_STORE}" == sqlite || "${ONLINE_STORE}" == redis ]] || { echo "ERROR: RAG_ISOLATED_ONLINE_STORE must be sqlite or redis" >&2; exit 2; }
[[ "${DATAHUB_MODE}" == plan_only || -n "${DATAHUB_GMS_URL:-}" ]] || { echo "ERROR: publish needs DATAHUB_GMS_URL" >&2; exit 2; }
ISOLATED_HOME="$(mktemp -d "${TMPDIR:-/tmp}/rag-dag-test.XXXXXX")"
trap 'rm -rf "${ISOLATED_HOME}"' EXIT

test -x "${RAG_PY}" || {
  echo "ERROR: run: uv sync --locked --no-dev --project infra/docker/rag-ingestion" >&2; exit 1; }

if [[ "${ONLINE_STORE}" == redis ]]; then
  RUN_ENV_FILE="${OPERATOR_ENV_FILE}"
  PUBLICATION_BASE=""   # from the operator env file
else
  # An empty private env file keeps an operator's runtime.env out of a local-dev run.
  RUN_ENV_FILE="${ISOLATED_HOME}/runtime.env"
  install -m 600 /dev/null "${RUN_ENV_FILE}"
  PUBLICATION_BASE="${ISOLATED_HOME}/feast"
fi

cd "${ROOT_DIR}"
run_airflow() {
  env -i HOME="${HOME}" PATH=/usr/bin:/bin PYTHONPATH="${ROOT_DIR}" TMPDIR="${TMPDIR:-/tmp}" \
    HF_HOME="${HF_HOME:-${HOME}/.cache/ewallet-rag-hf}" \
    AIRFLOW_HOME="${ISOLATED_HOME}" \
    AIRFLOW__CORE__DAGS_FOLDER="${ROOT_DIR}/data_platform/orchestration/airflow/dags" \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///${ISOLATED_HOME}/isolated.db" \
    AIRFLOW__LOGGING__BASE_LOG_FOLDER="${ISOLATED_HOME}/logs" \
    ${PUBLICATION_BASE:+EWALLET_RAG_PUBLICATION_BASE="${PUBLICATION_BASE}"} \
    EWALLET_RAG_EMBED_THREADS=2 \
    DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-}" EWALLET_RAG_ENV_FILE="${RUN_ENV_FILE}" \
    "${AIRFLOW_BIN}" "$@"
}

run_airflow db migrate >/dev/null
set +e
TEST_LOG="${ISOLATED_HOME}/dags-test.log"
run_airflow dags test "${DAG_ID}" \
  -c "{\"online_store\": \"${ONLINE_STORE}\", \"datahub_mode\": \"${DATAHUB_MODE}\", \"run_label\": \"${RUN_LABEL}\"}" \
  2>&1 | tee "${TEST_LOG}"
TEST_RC=${PIPESTATUS[0]}
set -e

# Preserve the real Airflow task-state readback before the throwaway metadata
# database is removed. Runtime values and credentials are never written here.
AIRFLOW_EVIDENCE="${ROOT_DIR}/docs/evidence/llm_agent/rag_${RUN_LABEL}/airflow"
mkdir -p "${AIRFLOW_EVIDENCE}"
cp "${TEST_LOG}" "${AIRFLOW_EVIDENCE}/dags-test.log"

clean_airflow_json() {
  local raw=$1 output=$2
  "${RAG_PY}" - "${raw}" "${output}" <<'PY'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
payload = next((line for line in reversed(lines) if line.startswith("[{")), "[]")
json.dump(json.loads(payload), open(sys.argv[2], "w", encoding="utf-8"), indent=2)
PY
}

DAG_RUNS_RAW="${ISOLATED_HOME}/dag-runs.raw"
run_airflow dags list-runs "${DAG_ID}" -o json > "${DAG_RUNS_RAW}" 2>/dev/null || true
clean_airflow_json "${DAG_RUNS_RAW}" "${AIRFLOW_EVIDENCE}/dag_runs.json"
RUN_ID=$("${RAG_PY}" - "${AIRFLOW_EVIDENCE}/dag_runs.json" <<'PY'
import json, sys
try:
    runs = json.load(open(sys.argv[1], encoding="utf-8"))
    print(runs[-1]["run_id"] if runs else "")
except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
    print("")
PY
)
if [[ -n "${RUN_ID}" ]]; then
  printf '%s\n' "${RUN_ID}" > "${AIRFLOW_EVIDENCE}/run_id.txt"
  TASK_STATES_RAW="${ISOLATED_HOME}/task-states.raw"
  run_airflow tasks states-for-dag-run "${DAG_ID}" "${RUN_ID}" -o json \
    > "${TASK_STATES_RAW}" 2>/dev/null || true
  clean_airflow_json "${TASK_STATES_RAW}" "${AIRFLOW_EVIDENCE}/task_states.json"
fi
git -C "${ROOT_DIR}" rev-parse HEAD > "${AIRFLOW_EVIDENCE}/git_sha.txt"
for task in discover_documents parse_and_chunk generate_embeddings publish_feast_features \
            validate_online_readback publish_datahub_lineage; do
  find "${ISOLATED_HOME}/logs" -path "*/task_id=${task}/*.log" -type f -print0 2>/dev/null \
    | xargs -0 -r cat > "${AIRFLOW_EVIDENCE}/${task}.log"
done
exit "${TEST_RC}"
