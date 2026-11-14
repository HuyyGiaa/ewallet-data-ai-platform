#!/usr/bin/env bash
# Canonical run of the RAG ingestion DAG (rag_fraud_policy_ingestion) for evidence capture.
#
#   rag_runtime_run.sh preflight              [READ-ONLY]  every check prints PASS or FAIL
#   rag_runtime_run.sh prepare                [LOCAL]      locked runtime, model cache, settings file;
#                                                          RAG_TARGET=local also starts the local Redis
#   rag_runtime_run.sh start                  [LOCAL]      DataHub quickstart (if not healthy) + isolated Airflow
#   rag_runtime_run.sh trigger RUN_LABEL      [WRITES]     new DagRun; Feast/Redis writes go to the target only
#   rag_runtime_run.sh collect RUN_LABEL      [READ-ONLY]  Airflow states and logs, DataHub, Feast and Redis readback
#   rag_runtime_run.sh show VIEW RUN_LABEL    [READ-ONLY]  terminal view for screenshots: feast | online | summary
#   rag_runtime_run.sh stop [--with-datahub]  [LOCAL]      stops what start/prepare started; volumes are kept
#
# RAG_TARGET selects the Feast Online target:
#   local (default)  Redis 7.4 container ewallet-rag-redis on 127.0.0.1:16381 (DB 3, noeviction, ephemeral),
#                    Feast registry and offline Parquet under ~/ewallet-evidence/rag/feast; no cloud access.
#   gke              [CLOUD] the GKE Feast Online Redis through a kubectl port-forward on 127.0.0.1:16380
#                    (RAG_KUBE_CONTEXT required) and GCS paths, from ~/.config/ewallet-rag/runtime.env, which
#                    the owner writes. It needs an awake cluster; nothing here wakes it.
# Evidence: ~/ewallet-evidence/rag/rag_<RUN_LABEL>/ (EWALLET_RAG_EVIDENCE_ROOT).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DAG_ID=rag_fraud_policy_ingestion
TASKS=(discover_documents parse_and_chunk generate_embeddings publish_feast_features validate_online_readback publish_datahub_lineage)
TARGET="${RAG_TARGET:-local}"
AIRFLOW="${ROOT}/scripts/llm_agent/rag_isolated_airflow.sh"
export RAG_AIRFLOW_HOME="${RAG_AIRFLOW_HOME:-${HOME}/.cache/ewallet-rag-airflow}"
export RAG_AIRFLOW_PORT="${RAG_AIRFLOW_PORT:-8095}"
export EWALLET_RAG_EVIDENCE_ROOT="${EWALLET_RAG_EVIDENCE_ROOT:-${HOME}/ewallet-evidence/rag}"
export HF_HOME="${HF_HOME:-${HOME}/.cache/ewallet-rag-hf}"
case "${TARGET}" in
  local) export EWALLET_RAG_ENV_FILE="${EWALLET_RAG_ENV_FILE:-${HOME}/.config/ewallet-rag/runtime.local.env}" ;;
  gke) export EWALLET_RAG_ENV_FILE="${EWALLET_RAG_ENV_FILE:-${HOME}/.config/ewallet-rag/runtime.env}" ;;
  *) echo "RAG_TARGET must be local or gke" >&2; exit 2 ;;
esac
RAG_PY="${ROOT}/infra/docker/rag-ingestion/.venv/bin/python"
UV="${UV:-$(command -v uv || echo "${HOME}/miniconda3/envs/fintech/bin/uv")}"
LOCAL_REDIS=ewallet-rag-redis
LOCAL_REDIS_PORT="${RAG_LOCAL_REDIS_PORT:-16381}"
LOCAL_REDIS_DB=3
LOCAL_PROJECT=ewallet_rag_verify
GKE_FORWARD_PORT="${RAG_REDIS_LOCAL_PORT:-16380}"
DATAHUB_GMS="http://127.0.0.1:18080"
FORWARD_PID=""
FAILED=0

die() { echo "ERROR: $*" >&2; exit 1; }
af() { "${AIRFLOW}" airflow "$@" 2>/dev/null; }
env_value() { sed -n "s/^$1=//p" "${EWALLET_RAG_ENV_FILE}" 2>/dev/null | tail -1; }
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; FAILED=1; fi; }
run_dir() { echo "${EWALLET_RAG_EVIDENCE_ROOT}/rag_$1"; }
valid_label() { [[ "$1" =~ ^[a-z0-9][a-z0-9_-]{2,63}$ ]] || die "RUN_LABEL must match ^[a-z0-9][a-z0-9_-]{2,63}$"; }
py() { PYTHONPATH="${ROOT}" "${RAG_PY}" "$@"; }

start_forward() {
  [[ "${TARGET}" == gke ]] || return 0
  [[ -n "${RAG_KUBE_CONTEXT:-}" ]] || die "RAG_TARGET=gke needs RAG_KUBE_CONTEXT"
  # [CLOUD, read path] a port-forward only; the cluster must already be awake.
  kubectl --context "${RAG_KUBE_CONTEXT}" -n feature-store port-forward svc/redis \
    "${GKE_FORWARD_PORT}:6379" >/dev/null 2>&1 &
  FORWARD_PID=$!
  trap '[[ -n "${FORWARD_PID}" ]] && kill "${FORWARD_PID}" 2>/dev/null || true' EXIT
  for _ in $(seq 1 15); do ss -ltn | grep -q "127.0.0.1:${GKE_FORWARD_PORT} " && return 0; sleep 1; done
  die "port-forward to feature-store/svc/redis did not open"
}

# Python helper: resolve the configured target without printing the connection string.
target_report() {
  py - <<'EOF'
import json, os
from ops.llm_agent import rag_ingest
from ai_platform.llm_agent.rag import online_target
from ai_platform.features.feast import redis_isolation
rag_ingest.load_env_file()
project = os.environ.get("EWALLET_RAG_FEAST_PROJECT", "") or "ewallet_rag"
resolved = online_target.resolve(os.environ["EWALLET_REDIS_CONNECTION_STRING"], project=project, environment=os.environ)
client = online_target.redis_client(resolved.endpoint, timeout_seconds=5)
client.ping()
owner = redis_isolation.read_owner(client) or {}
print(json.dumps({**resolved.describe(), "dbsize": client.dbsize(), "owner_campaign": owner.get("campaign_id"),
                  "maxmemory_policy": client.config_get("maxmemory-policy").get("maxmemory-policy")
                  if resolved.endpoint.host in ("127.0.0.1", "localhost") else "not-read"}))
EOF
}

target_is_usable() {
  local report; report=$(target_report) || return 1
  py -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if r["dbsize"]==0 or r["owner_campaign"]==r["project"] else 1)' "${report}"
}

preflight() {
  FAILED=0
  cd "${ROOT}"
  echo "target=${TARGET} env_file=${EWALLET_RAG_ENV_FILE} evidence=${EWALLET_RAG_EVIDENCE_ROOT}"
  check "checkout is clean ($(git rev-parse --short HEAD))" "test -z \"\$(git status --short --untracked-files=no)\""
  check "exactly one RAG DAG, ${DAG_ID}, with the six tasks" \
    "test \"\$(ls data_platform/orchestration/airflow/dags/rag_*.py)\" = data_platform/orchestration/airflow/dags/${DAG_ID}.py && grep -q '\"publish_datahub_lineage\", \"datahub\"' data_platform/orchestration/airflow/dags/${DAG_ID}.py"
  check "uv lock consistent (infra/docker/rag-ingestion)" "\"${UV}\" lock --check --project infra/docker/rag-ingestion"
  check "locked task runtime present (${RAG_PY})" "\"${RAG_PY}\" -c 'import feast, onnxruntime, datahub, redis'"
  check "embedding model cached in ${HF_HOME}" "test -d \"${HF_HOME}/models--sentence-transformers--all-MiniLM-L6-v2\""
  check "settings file exists and is private (chmod 600)" "test -f \"${EWALLET_RAG_ENV_FILE}\" && [ \"\$(stat -c %a \"${EWALLET_RAG_ENV_FILE}\")\" = 600 ]"
  for key in EWALLET_RAG_FEAST_REGISTRY EWALLET_RAG_FEAST_PROJECT EWALLET_REDIS_CONNECTION_STRING EWALLET_RAG_PUBLICATION_BASE DATAHUB_GMS_URL; do
    check "settings set ${key}" "test -n \"\$(env_value ${key})\""
  done
  start_forward
  check "Redis target is explicit, isolated and reachable: $(target_report 2>/dev/null || echo unreachable)" "target_report"
  check "Redis database is empty or already owned by this project" "target_is_usable"
  check "DataHub GMS healthy ($(env_value DATAHUB_GMS_URL))" "curl -fsS --max-time 10 \"\$(env_value DATAHUB_GMS_URL)/health\""
  check "DataHub OpenSearch indices writable (no read-only block)" \
    "! curl -fsS --max-time 10 'http://127.0.0.1:9200/_all/_settings/index.blocks.read_only_allow_delete' | grep -q '\"true\"'"
  check "isolated Airflow healthy on 127.0.0.1:${RAG_AIRFLOW_PORT}" "curl -fsS --max-time 10 http://127.0.0.1:${RAG_AIRFLOW_PORT}/api/v2/monitor/health"
  check "isolated Airflow parsed ${DAG_ID} without import errors" \
    "af dags list -o plain | grep -q '^${DAG_ID} ' && ! af dags list-import-errors -o plain | grep -q '${DAG_ID}'"
  check "isolated Airflow runs this checkout's DAG file" \
    "test \"\$(readlink -f \"${RAG_AIRFLOW_HOME}/dags/${DAG_ID}.py\")\" = \"${ROOT}/data_platform/orchestration/airflow/dags/${DAG_ID}.py\""
  check "free disk above 10 GiB" "test \$(df --output=avail -BG / | tail -1 | tr -dc 0-9) -gt 10"
  if [[ "${TARGET}" == gke ]]; then
    check "GCS publication base listable" "gcloud storage ls \"\$(env_value EWALLET_RAG_PUBLICATION_BASE | sed 's#/*\$##' | xargs dirname)\""
  fi
  return "${FAILED}"
}

write_local_settings() {
  local feast="${EWALLET_RAG_EVIDENCE_ROOT}/feast/${LOCAL_PROJECT}"
  install -d -m 700 "$(dirname "${EWALLET_RAG_ENV_FILE}")"
  if [[ -f "${EWALLET_RAG_ENV_FILE}" ]]; then echo "kept existing ${EWALLET_RAG_ENV_FILE}"; return; fi
  mkdir -p "${feast}"
  install -m 600 /dev/null "${EWALLET_RAG_ENV_FILE}"
  cat >"${EWALLET_RAG_ENV_FILE}" <<EOF
# Local canonical-profile target written by rag_runtime_run.sh prepare (no credentials).
EWALLET_RAG_ONLINE_TARGET=verification
EWALLET_RAG_FEAST_PROJECT=${LOCAL_PROJECT}
EWALLET_RAG_FEAST_REGISTRY=${feast}/registry.db
EWALLET_RAG_PUBLICATION_BASE=${feast}/publication
EWALLET_REDIS_CONNECTION_STRING=127.0.0.1:${LOCAL_REDIS_PORT},db=${LOCAL_REDIS_DB}
DATAHUB_GMS_URL=${DATAHUB_GMS}
HF_HOME=${HF_HOME}
EWALLET_RAG_EMBED_THREADS=2
EOF
  echo "wrote ${EWALLET_RAG_ENV_FILE}"
}

prepare() {
  cd "${ROOT}"
  "${UV}" sync --locked --no-dev --project infra/docker/rag-ingestion
  # Downloads the pinned ONNX model into HF_HOME once; later runs read the cache.
  py - <<'EOF'
import json
from pathlib import Path
from ops.llm_agent import rag_ingest
from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex
index = TrustedChunkIndex.from_manifest(Path("data/llm_agent/rag/fraud-policy-v1/manifest.json"))
vector = rag_ingest._embedder(index.manifest)(["model cache check"])[0]
print(json.dumps({"model": index.manifest.embedding_model_id, "revision": index.manifest.embedding_model_revision,
                  "dimensions": len(vector)}))
EOF
  if [[ "${TARGET}" == local ]]; then
    write_local_settings
    if docker ps --format '{{.Names}}' | grep -qx "${LOCAL_REDIS}"; then
      echo "${LOCAL_REDIS} already running"
    else
      ss -ltn | grep -q "127.0.0.1:${LOCAL_REDIS_PORT} " && die "port ${LOCAL_REDIS_PORT} is in use"
      docker run -d --rm --name "${LOCAL_REDIS}" -p "127.0.0.1:${LOCAL_REDIS_PORT}:6379" \
        --label ewallet.rag/purpose=local-verification \
        redis:7.4.2-alpine redis-server --maxmemory 256mb --maxmemory-policy noeviction >/dev/null
      echo "started ${LOCAL_REDIS} on 127.0.0.1:${LOCAL_REDIS_PORT} (ephemeral; data ends with stop)"
    fi
  else
    test -f "${EWALLET_RAG_ENV_FILE}" || die "write ${EWALLET_RAG_ENV_FILE} first (runbook, GKE target)"
    echo "gke target: settings are the owner's ${EWALLET_RAG_ENV_FILE}; nothing started"
  fi
}

start() {
  cd "${ROOT}"
  if curl -fsS --max-time 5 "${DATAHUB_GMS}/health" >/dev/null 2>&1; then
    echo "DataHub already healthy at ${DATAHUB_GMS}"
  else
    local free; free=$(free -g | awk '/^Mem:/{print $7}')
    (( free >= 6 )) || die "DataHub needs about 5 GiB; only ${free} GiB available"
    DATAHUB_PYTHON="${DATAHUB_PYTHON:-${HOME}/miniconda3/envs/datahub/bin/python}" DATAHUB_PULL_IMAGES=false \
      DATAHUB_MAPPED_GMS_PORT=18080 ./data_platform/metadata/datahub/runtime/start_datahub.sh start
  fi
  echo "${TARGET}" > "${RAG_AIRFLOW_HOME}.target" 2>/dev/null || true
  "${AIRFLOW}" start
  af dags unpause "${DAG_ID}" >/dev/null && echo "unpaused ${DAG_ID}; UI http://127.0.0.1:${RAG_AIRFLOW_PORT}"
}

collect() {
  local label=$1 dir run_id
  valid_label "${label}"
  dir=$(run_dir "${label}")
  test -f "${dir}/airflow/run_id.txt" || die "no run_id.txt in ${dir}/airflow (was the run triggered here?)"
  run_id=$(<"${dir}/airflow/run_id.txt")
  git -C "${ROOT}" rev-parse HEAD > "${dir}/airflow/git_sha.txt"
  af dags list-runs "${DAG_ID}" -o json > "${dir}/airflow/dag_runs.json" || true
  af tasks states-for-dag-run "${DAG_ID}" "${run_id}" -o json > "${dir}/airflow/task_states.json" || true
  for task in "${TASKS[@]}"; do
    find "${RAG_AIRFLOW_HOME}/logs/dag_id=${DAG_ID}/run_id=${run_id}/task_id=${task}" -name '*.log' \
      -exec sh -c 'for f; do echo "### $f"; cat "$f"; done' _ {} + > "${dir}/airflow/${task}.log" 2>/dev/null || true
  done
  start_forward
  if [[ -f "${dir}/publication.json" ]]; then
    [[ -f "${dir}/feast_registry.json" ]] || py -m ops.llm_agent.rag_evidence feast --run-dir "${dir}" --json "${dir}/feast_registry.json" >/dev/null
    [[ -f "${dir}/online_readback.json" ]] || py -m ops.llm_agent.rag_evidence online --run-dir "${dir}" --json "${dir}/online_readback.json" >/dev/null
  fi
  # Raw DataHub readback of the lineage edge and the flow (independent of the task's own readback).
  if [[ -f "${dir}/datahub.json" && ! -f "${dir}/datahub_raw_readback.json" ]]; then
    py - "${dir}" "$(env_value DATAHUB_GMS_URL)" "${DAG_ID}" <<'EOF'
import json, sys, urllib.parse, urllib.request
directory, gms, dag = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3]
summary = json.load(open(f"{directory}/datahub.json"))
urns = {summary["feast_dataset"]: "upstreamLineage", f"urn:li:dataFlow:(airflow,{dag},ewallet-gcp)": "dataFlowInfo"}
out = {}
for urn, aspect in urns.items():
    url = f"{gms}/aspects/{urllib.parse.quote(urn, safe='')}?aspect={aspect}&version=0"
    with urllib.request.urlopen(url, timeout=15) as response:
        out[f"{urn}#{aspect}"] = json.load(response)
json.dump(out, open(f"{directory}/datahub_raw_readback.json", "w"), indent=2)
EOF
  fi
  py -m ops.llm_agent.rag_evidence summary --run-dir "${dir}" > "${dir}/summary.txt"
  (cd "${dir}" && find . -type f ! -name SHA256SUMS ! -path './source/*' | sort | xargs sha256sum > SHA256SUMS)
  echo "collected into ${dir}"
}

trigger() {
  local label=$1 dir run_id state=unknown
  valid_label "${label}"
  dir=$(run_dir "${label}")
  [[ ! -e "${dir}" ]] || die "evidence dir exists: ${dir}; use a new RUN_LABEL"
  [[ "$(cat "${RAG_AIRFLOW_HOME}.target" 2>/dev/null)" == "${TARGET}" ]] \
    || die "the isolated Airflow was started for another RAG_TARGET; run stop, then start with RAG_TARGET=${TARGET}"
  preflight || die "preflight failed; nothing was triggered"
  run_id="manual__rag-${label}"
  af dags trigger "${DAG_ID}" --run-id "${run_id}" \
    --conf "{\"online_store\": \"redis\", \"datahub_mode\": \"publish\", \"run_label\": \"${label}\"}" >/dev/null
  mkdir -p "${dir}/airflow" && echo "${run_id}" > "${dir}/airflow/run_id.txt"
  for _ in $(seq 1 90); do   # bounded: 30 minutes
    state=$(af dags list-runs "${DAG_ID}" -o json | "${RAG_PY}" -c \
      "import json,sys;print(next((r['state'] for r in json.load(sys.stdin) if r['run_id']=='${run_id}'),'unknown'))")
    echo "$(date -u +%FT%TZ) ${run_id} ${state}"
    [[ "${state}" == success || "${state}" == failed ]] && break
    sleep 20
  done
  collect "${label}"
  [[ "${state}" == success ]]
}

show() {
  local view=${1:?feast|online|summary} label=${2:?RUN_LABEL}
  valid_label "${label}"
  start_forward
  py -m ops.llm_agent.rag_evidence "${view}" --run-dir "$(run_dir "${label}")"
}

stop() {
  "${AIRFLOW}" stop || true
  rm -f "${RAG_AIRFLOW_HOME}.target"
  if docker ps --format '{{.Names}}' | grep -qx "${LOCAL_REDIS}"; then
    docker stop "${LOCAL_REDIS}" >/dev/null && echo "stopped ${LOCAL_REDIS}"
  fi
  if [[ "${1:-}" == --with-datahub ]]; then
    ./data_platform/metadata/datahub/runtime/start_datahub.sh stop   # containers only; volumes are kept
  fi
}

case "${1:-}" in
  preflight) preflight ;;
  prepare) prepare ;;
  start) start ;;
  trigger) trigger "${2:?RUN_LABEL}" ;;
  collect) collect "${2:?RUN_LABEL}" ;;
  show) show "${2:-}" "${3:-}" ;;
  stop) cd "${ROOT}" && stop "${2:-}" ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
