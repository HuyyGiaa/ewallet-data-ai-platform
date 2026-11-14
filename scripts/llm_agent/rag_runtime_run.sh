#!/usr/bin/env bash
# Canonical run of the RAG ingestion DAG (rag_fraud_policy_ingestion) for evidence capture.
#
#   rag_runtime_run.sh preflight              [READ-ONLY]  every check prints PASS or FAIL
#   rag_runtime_run.sh prepare                [LOCAL]      locked runtime, model cache; RAG_TARGET=local also
#                                                          writes its settings and starts the local Redis
#   rag_runtime_run.sh start                  [LOCAL]      DataHub quickstart (if not healthy) + isolated Airflow
#   rag_runtime_run.sh trigger RUN_LABEL      [WRITES]     new DagRun; Feast/Redis writes go to the target only
#   rag_runtime_run.sh collect RUN_LABEL      [READ-ONLY]  Airflow states and logs, DataHub, Feast and Redis readback
#   rag_runtime_run.sh show VIEW RUN_LABEL    [READ-ONLY]  terminal view for screenshots: feast | online | summary
#   rag_runtime_run.sh stop [--with-datahub]  [LOCAL]      stops what start/prepare/forward started; volumes are kept
# gke target only:
#   rag_runtime_run.sh dbs                    [CLOUD, READ-ONLY]  size and owner of Redis DB 0-15
#   rag_runtime_run.sh configure DB PROJECT [--replace]
#                                             [CLOUD read, LOCAL write]  writes the gke settings file (mode 600)
#   rag_runtime_run.sh forward                [CLOUD, READ-ONLY]  holds the Redis port-forward until Ctrl-C
#
# RAG_TARGET selects the Feast Online target:
#   local (default)  Redis 7.4 container ewallet-rag-redis on 127.0.0.1:16381 (DB 3, noeviction, ephemeral),
#                    Feast registry and offline Parquet under ~/ewallet-evidence/rag/feast; no cloud access.
#   gke              [CLOUD] the GKE Feast Online Redis (feature-store/svc/redis) through a kubectl
#                    port-forward on 127.0.0.1:16380 (RAG_KUBE_CONTEXT, KUBECONFIG), Feast registry and
#                    Parquet on GCS, from ~/.config/ewallet-rag/runtime.env. Needs an awake cluster;
#                    nothing here wakes it. Writes only a verification DB in 2-15, never DB 0 or 1.
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
RAG_PY="${RAG_PY:-${ROOT}/infra/docker/rag-ingestion/.venv/bin/python}"
UV="${UV:-$(command -v uv || echo "${HOME}/miniconda3/envs/fintech/bin/uv")}"
MANIFEST=data/llm_agent/rag/fraud-policy-v1/manifest.json
LOCAL_REDIS=ewallet-rag-redis
LOCAL_REDIS_PORT="${RAG_LOCAL_REDIS_PORT:-16381}"
LOCAL_REDIS_DB=3
LOCAL_PROJECT=ewallet_rag_verify
GKE_FORWARD_PORT="${RAG_REDIS_LOCAL_PORT:-16380}"
GKE_NAMESPACE=feature-store
GKE_BUCKET="${RAG_GCS_BUCKET:-ewallet-data-ai-platform-hg26-data}"
# One forward per laptop: the PID file records the runner invocation that owns it.
FORWARD_PID_FILE="${RAG_AIRFLOW_HOME}.forward.pid"
FORWARD_LOG="${RAG_AIRFLOW_HOME}.forward.log"
DATAHUB_GMS="http://127.0.0.1:18080"
FORWARD_PID=""
FORWARD_OWNED=0
FAILED=0
REPORT=""

die() { echo "ERROR: $*" >&2; exit 1; }
af() { "${AIRFLOW}" airflow "$@" 2>/dev/null; }
# The Airflow CLI prints warnings on stdout before its JSON; keep the JSON line only.
af_json() { af "$@" -o json | grep -E '^\[' | tail -1; }
env_value() { sed -n "s/^$1=//p" "${EWALLET_RAG_ENV_FILE}" 2>/dev/null | tail -1; }
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; FAILED=1; fi; }
run_dir() { echo "${EWALLET_RAG_EVIDENCE_ROOT}/rag_$1"; }
valid_label() { [[ "$1" =~ ^[a-z0-9][a-z0-9_-]{2,63}$ ]] || die "RUN_LABEL must match ^[a-z0-9][a-z0-9_-]{2,63}$"; }
py() { PYTHONPATH="${ROOT}" "${RAG_PY}" "$@"; }
kc() { kubectl --context "${RAG_KUBE_CONTEXT}" "$@"; }

# --- Kubernetes port-forward to the GKE Redis --------------------------------------------------------
# Lifecycle: within one invocation start_forward is idempotent (preflight, the trigger wait and collect
# share one forward). A forward recorded in FORWARD_PID_FILE by another invocation is reused only if its
# command line is exactly this runner's forward; it is then not closed here. A port held by anything else
# stops the run. The owning invocation closes its forward on exit (also on Ctrl-C/TERM); `stop` closes a
# recorded forward left behind by a killed invocation.

port_listeners() { ss -Hltnp "sport = :$1" 2>/dev/null; }
is_runner_forward() {
  local pid=$1
  [[ -n "${pid}" && -r "/proc/${pid}/cmdline" ]] || return 1
  local cmdline; cmdline=$(tr '\0' ' ' <"/proc/${pid}/cmdline")
  [[ "${cmdline}" == *"--context ${RAG_KUBE_CONTEXT:-} "* \
     && "${cmdline}" == *"port-forward --address 127.0.0.1 svc/redis ${GKE_FORWARD_PORT}:6379"* ]]
}
forward_listening() { port_listeners "${GKE_FORWARD_PORT}" | grep -q "pid=$1,"; }

close_forward() {
  # An owned forward is this shell's own child ($!), so it is stopped without a command-line check.
  if (( FORWARD_OWNED )) && [[ -n "${FORWARD_PID}" ]]; then
    kill "${FORWARD_PID}" 2>/dev/null || true
    wait "${FORWARD_PID}" 2>/dev/null || true
    [[ "$(cat "${FORWARD_PID_FILE}" 2>/dev/null)" == "${FORWARD_PID}" ]] && rm -f "${FORWARD_PID_FILE}"
  fi
  FORWARD_PID=""
  FORWARD_OWNED=0
}

start_forward() {
  [[ "${TARGET}" == gke ]] || return 0
  [[ -n "${FORWARD_PID}" ]] && is_runner_forward "${FORWARD_PID}" && forward_listening "${FORWARD_PID}" && return 0
  [[ -n "${FORWARD_PID}" ]] && { echo "port-forward pid ${FORWARD_PID} is gone; reopening" >&2; close_forward; }
  [[ -n "${RAG_KUBE_CONTEXT:-}" ]] || { echo "RAG_TARGET=gke needs RAG_KUBE_CONTEXT" >&2; return 1; }
  local recorded; recorded=$(cat "${FORWARD_PID_FILE}" 2>/dev/null || true)
  if is_runner_forward "${recorded}" && forward_listening "${recorded}"; then
    FORWARD_PID=${recorded}; FORWARD_OWNED=0   # another runner invocation owns it
    return 0
  fi
  if [[ -n "$(port_listeners "${GKE_FORWARD_PORT}")" ]]; then
    echo "127.0.0.1:${GKE_FORWARD_PORT} is held by another process; nothing was started:" >&2
    port_listeners "${GKE_FORWARD_PORT}" >&2
    echo "close it (pgrep -a kubectl | grep port-forward; kill <pid>) or set RAG_REDIS_LOCAL_PORT" >&2
    return 1
  fi
  # [CLOUD, read path] a port-forward only; the cluster must already be awake.
  # kubectl itself, not the kc function: $! must be the kubectl process, not a subshell around it.
  kubectl --context "${RAG_KUBE_CONTEXT}" -n "${GKE_NAMESPACE}" port-forward --address 127.0.0.1 svc/redis \
    "${GKE_FORWARD_PORT}:6379" >"${FORWARD_LOG}" 2>&1 &
  FORWARD_PID=$!
  FORWARD_OWNED=1
  echo "${FORWARD_PID}" >"${FORWARD_PID_FILE}"
  for _ in $(seq 1 20); do
    forward_listening "${FORWARD_PID}" && return 0
    kill -0 "${FORWARD_PID}" 2>/dev/null || break
    sleep 1
  done
  echo "port-forward to ${GKE_NAMESPACE}/svc/redis did not open: $(tail -3 "${FORWARD_LOG}" 2>/dev/null)" >&2
  close_forward
  return 1
}

stop_recorded_forward() {
  local recorded; recorded=$(cat "${FORWARD_PID_FILE}" 2>/dev/null || true)
  if is_runner_forward "${recorded}"; then kill "${recorded}" && echo "stopped port-forward pid ${recorded}"; fi
  rm -f "${FORWARD_PID_FILE}"
}

redis_secret_password() {
  # [CLOUD, READ-ONLY] the password of the GKE Redis; held in memory only, never printed.
  kc -n "${GKE_NAMESPACE}" get secret redis -o jsonpath='{.data.password}' | base64 -d
}

# --- Target report (no connection string or password is ever printed) -------------------------------
target_report() {
  (cd "${ROOT}" && py - "${MANIFEST}" <<'EOF'
import hashlib, json, os, sys
from pathlib import Path
report = {}
try:
    from ops.llm_agent import rag_ingest
    from ai_platform.llm_agent.rag import online_target
    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex
    rag_ingest.load_env_file()
    env = os.environ
    project = env.get("EWALLET_RAG_FEAST_PROJECT", "") or "ewallet_rag"
    resolved = online_target.resolve(env.get("EWALLET_REDIS_CONNECTION_STRING", ""), project=project, environment=env)
    report.update(resolved.describe())
    online_target.check_storage_paths(resolved, registry=env.get("EWALLET_RAG_FEAST_REGISTRY", ""),
                                      publication_base=env.get("EWALLET_RAG_PUBLICATION_BASE", ""))
    report["storage_paths"] = "DEDICATED"
    client = online_target.redis_client(resolved.endpoint, timeout_seconds=5)
    client.ping()
    manifest_path = Path(sys.argv[1])
    manifest = TrustedChunkIndex.from_manifest(manifest_path).manifest
    report.update(online_target.preview_ownership(
        resolved, client, registry=env["EWALLET_RAG_FEAST_REGISTRY"], corpus_revision=manifest.corpus_revision,
        feature_index_revision=manifest.feature_index_revision,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()))
    try:
        report["maxmemory_policy"] = client.config_get("maxmemory-policy").get("maxmemory-policy")
    except Exception as error:  # CONFIG may be restricted; the check then fails closed
        report["maxmemory_policy"] = f"unreadable ({type(error).__name__})"
    report["status"] = "OK"
except Exception as error:
    report = {**report, "status": "ERROR", "error": f"{type(error).__name__}: {error}"}
print(json.dumps(report, sort_keys=True))
EOF
  )
}

gcs_report() {
  # [CLOUD, READ-ONLY] testIamPermissions with the Application Default Credentials Feast uses; writes nothing.
  py - <<'EOF'
import json, os, sys
from ops.llm_agent import rag_ingest
rag_ingest.load_env_file()
needed = ["storage.objects.create", "storage.objects.delete", "storage.objects.get", "storage.objects.list"]
try:
    import google.auth
    from google.cloud import storage
    credentials, project = google.auth.default()
    client = storage.Client(credentials=credentials, project=project or "ewallet-data-ai-platform-hg26")
    out = {}
    for name in ("EWALLET_RAG_FEAST_REGISTRY", "EWALLET_RAG_PUBLICATION_BASE"):
        uri = os.environ.get(name, "")
        if not uri.startswith("gs://"):
            out[name] = {"error": "not a gs:// path"}
            continue
        bucket = uri[5:].split("/", 1)[0]
        granted = client.bucket(bucket).test_iam_permissions(needed)
        out[name] = {"bucket": bucket, "missing": sorted(set(needed) - set(granted))}
except Exception as error:
    out = {"error": f"{type(error).__name__}: {error}"}
print(json.dumps(out, sort_keys=True))
sys.exit(0 if "error" not in out and all(not v.get("missing") and "error" not in v for v in out.values()) else 1)
EOF
}

report_ok() { py -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "${REPORT}" "$1"; }

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
  check "settings select verification mode (this runner never writes production DB 0)" \
    "test \"\$(env_value EWALLET_RAG_ONLINE_TARGET)\" = verification"
  if [[ "${TARGET}" == gke ]]; then
    check "kube context ${RAG_KUBE_CONTEXT:-<unset>} in ${KUBECONFIG:-~/.kube/config}" \
      "kubectl config get-contexts -o name | grep -qx \"\${RAG_KUBE_CONTEXT}\""
    check "[CLOUD] GKE API reachable and ${GKE_NAMESPACE}/redis ready 1/1" \
      "test \"\$(kc -n ${GKE_NAMESPACE} get statefulset redis -o jsonpath='{.status.readyReplicas}' --request-timeout=20s)\" = 1"
    check "[CLOUD] port-forward 127.0.0.1:${GKE_FORWARD_PORT} -> ${GKE_NAMESPACE}/svc/redis (one, runner-owned)" "start_forward"
  fi
  REPORT=$(target_report 2>/dev/null || echo '{"status": "ERROR"}')
  echo "      target report: ${REPORT}"
  check "Redis target is explicit, isolated and reachable (verification DB 2-15, project ewallet_rag_<suffix>)" \
    "report_ok 'r[\"status\"] == \"OK\" and r[\"mode\"] == \"verification\"'"
  check "Feast registry and publication base are dedicated to the project" \
    "report_ok 'r.get(\"storage_paths\") == \"DEDICATED\"'"
  check "Redis database is free or already owned by this exact publication (CLAIMABLE_EMPTY | OWNED_MATCH)" \
    "report_ok 'r.get(\"ownership\") in (\"CLAIMABLE_EMPTY\", \"OWNED_MATCH\")'"
  check "Redis eviction policy is noeviction" "report_ok 'r.get(\"maxmemory_policy\") == \"noeviction\"'"
  if [[ "${TARGET}" == gke ]]; then
    local gcs gcs_ok=0
    gcs=$(gcs_report 2>/dev/null) && gcs_ok=1
    check "[CLOUD] ADC (used by Feast) may create, overwrite, read and list on the GCS paths: ${gcs}" "test ${gcs_ok} = 1"
  fi
  check "DataHub GMS healthy ($(env_value DATAHUB_GMS_URL))" "curl -fsS --max-time 10 \"\$(env_value DATAHUB_GMS_URL)/health\""
  check "DataHub OpenSearch indices writable (no read-only block)" \
    "! curl -fsS --max-time 10 'http://127.0.0.1:9200/_all/_settings/index.blocks.read_only_allow_delete' | grep -q '\"true\"'"
  check "isolated Airflow healthy on 127.0.0.1:${RAG_AIRFLOW_PORT}" "curl -fsS --max-time 10 http://127.0.0.1:${RAG_AIRFLOW_PORT}/api/v2/monitor/health"
  check "isolated Airflow was started for RAG_TARGET=${TARGET}" "test \"\$(cat \"${RAG_AIRFLOW_HOME}.target\")\" = ${TARGET}"
  check "isolated Airflow parsed ${DAG_ID} without import errors" \
    "af dags list -o plain | grep -q '^${DAG_ID} ' && ! af dags list-import-errors -o plain | grep -q '${DAG_ID}'"
  check "isolated Airflow runs this checkout's DAG file" \
    "test \"\$(readlink -f \"${RAG_AIRFLOW_HOME}/dags/${DAG_ID}.py\")\" = \"${ROOT}/data_platform/orchestration/airflow/dags/${DAG_ID}.py\""
  check "free disk above 10 GiB" "test \$(df --output=avail -BG / | tail -1 | tr -dc 0-9) -gt 10"
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

configure() {
  # Writes the gke settings: verification mode, DB, project, GCS paths keyed by the project, and the
  # Redis password read from the Secret feature-store/redis (never printed, never in argv).
  local db=${1:?DB} project=${2:?PROJECT} replace=${3:-} password tmp
  [[ "${TARGET}" == gke ]] || die "configure is for RAG_TARGET=gke; the local target is written by prepare"
  [[ -n "${RAG_KUBE_CONTEXT:-}" ]] || die "RAG_TARGET=gke needs RAG_KUBE_CONTEXT"
  local registry="gs://${GKE_BUCKET}/features/registries/rag/${project}/registry.db"
  local publication="gs://${GKE_BUCKET}/features/rag/${project}"
  local connection="127.0.0.1:${GKE_FORWARD_PORT},db=${db}"
  if ! EWALLET_RAG_ONLINE_TARGET=verification py - "${connection}" "${project}" "${registry}" "${publication}" <<'EOF'
import os, sys
from ai_platform.llm_agent.rag import online_target
connection, project, registry, publication = sys.argv[1:5]
try:
    target = online_target.resolve(connection, project=project, environment=os.environ)
    online_target.check_storage_paths(target, registry=registry, publication_base=publication)
except online_target.RagTargetError as error:
    sys.exit(f"refused: {error}")
EOF
  then
    die "DB ${db} / project ${project} is not a valid verification target"
  fi
  if [[ -e "${EWALLET_RAG_ENV_FILE}" ]]; then
    [[ "${replace}" == --replace ]] || die "${EWALLET_RAG_ENV_FILE} exists; rerun with --replace to keep it as a backup and write a new one"
  fi
  password=$(redis_secret_password) || die "could not read Secret ${GKE_NAMESPACE}/redis (is the cluster awake, KUBECONFIG set?)"
  [[ -n "${password}" ]] || die "Secret ${GKE_NAMESPACE}/redis has an empty password"
  [[ "${password}" != *[,[:space:]]* ]] || die "the Redis password contains ',' or whitespace, which the Feast connection string cannot carry"
  install -d -m 700 "$(dirname "${EWALLET_RAG_ENV_FILE}")"
  tmp=$(mktemp "${EWALLET_RAG_ENV_FILE}.XXXXXX")
  chmod 600 "${tmp}"
  cat >"${tmp}" <<EOF
# GKE (hybrid) target written by rag_runtime_run.sh configure on $(date -u +%FT%TZ). Contains a credential: never print or commit.
EWALLET_RAG_ONLINE_TARGET=verification
EWALLET_RAG_FEAST_PROJECT=${project}
EWALLET_RAG_FEAST_REGISTRY=${registry}
EWALLET_RAG_PUBLICATION_BASE=${publication}
EWALLET_REDIS_CONNECTION_STRING=127.0.0.1:${GKE_FORWARD_PORT},password=${password},db=${db}
DATAHUB_GMS_URL=${DATAHUB_GMS}
HF_HOME=${HF_HOME}
EWALLET_RAG_EMBED_THREADS=2
EOF
  if [[ -e "${EWALLET_RAG_ENV_FILE}" ]]; then
    local backup; backup="${EWALLET_RAG_ENV_FILE}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "${EWALLET_RAG_ENV_FILE}" "${backup}" && chmod 600 "${backup}" && echo "kept the previous settings as ${backup}"
  fi
  mv "${tmp}" "${EWALLET_RAG_ENV_FILE}"
  echo "wrote ${EWALLET_RAG_ENV_FILE}: verification, DB ${db}, project ${project}"
  echo "  registry    ${registry}"
  echo "  publication ${publication}"
}

dbs() {
  [[ "${TARGET}" == gke ]] || die "dbs is for RAG_TARGET=gke"
  [[ -n "${RAG_KUBE_CONTEXT:-}" ]] || die "RAG_TARGET=gke needs RAG_KUBE_CONTEXT"
  local password; password=$(redis_secret_password) || die "could not read Secret ${GKE_NAMESPACE}/redis"
  start_forward || die "no port-forward"
  RAG_REDIS_PASSWORD="${password}" py - "${GKE_FORWARD_PORT}" <<'EOF'
import os, sys
import redis
from ai_platform.features.feast import redis_isolation
password = os.environ.pop("RAG_REDIS_PASSWORD")
port = int(sys.argv[1])
roles = {0: "production (ewallet_rag, Feature Store)", 1: "canonical verification"}
info = redis.Redis(host="127.0.0.1", port=port, password=password, socket_timeout=10).info("keyspace")
print(f"{'db':>3}  {'keys':>8}  {'owner (campaign_id)':<34} use")
for db in range(16):
    keys = int(info.get(f"db{db}", {}).get("keys", 0))
    owner = "-"
    if keys:
        try:
            record = redis_isolation.read_owner(redis.Redis(host="127.0.0.1", port=port, db=db, password=password,
                                                            socket_timeout=10))
            owner = (record or {}).get("campaign_id") or "none (another workload)"
        except redis_isolation.RedisIsolationError:
            owner = "invalid marker"
    use = roles.get(db) or ("free for a new verification project" if keys == 0 else "in use")
    print(f"{db:>3}  {keys:>8}  {owner:<34} {use}")
EOF
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
    test -f "${EWALLET_RAG_ENV_FILE}" || die "write ${EWALLET_RAG_ENV_FILE} first: RAG_TARGET=gke $0 configure DB PROJECT"
    echo "gke target: settings are ${EWALLET_RAG_ENV_FILE}; nothing started"
  fi
}

start() {
  cd "${ROOT}"
  local running; running=$(cat "${RAG_AIRFLOW_HOME}.target" 2>/dev/null || true)
  if [[ -n "${running}" && "${running}" != "${TARGET}" ]] && "${AIRFLOW}" status 2>/dev/null | grep -q ': running'; then
    die "the isolated Airflow runs for RAG_TARGET=${running}; run '$0 stop' first (its tasks read that target's settings)"
  fi
  if curl -fsS --max-time 5 "${DATAHUB_GMS}/health" >/dev/null 2>&1; then
    echo "DataHub already healthy at ${DATAHUB_GMS}"
  else
    local free; free=$(free -g | awk '/^Mem:/{print $7}')
    (( free >= 6 )) || die "DataHub needs about 5 GiB; only ${free} GiB available"
    DATAHUB_PYTHON="${DATAHUB_PYTHON:-${HOME}/miniconda3/envs/datahub/bin/python}" DATAHUB_PULL_IMAGES=false \
      DATAHUB_MAPPED_GMS_PORT=18080 ./data_platform/metadata/datahub/runtime/start_datahub.sh start
  fi
  echo "${TARGET}" > "${RAG_AIRFLOW_HOME}.target"
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
  af_json dags list-runs "${DAG_ID}" > "${dir}/airflow/dag_runs.json" || true
  af_json tasks states-for-dag-run "${DAG_ID}" "${run_id}" > "${dir}/airflow/task_states.json" || true
  for task in "${TASKS[@]}"; do
    find "${RAG_AIRFLOW_HOME}/logs/dag_id=${DAG_ID}/run_id=${run_id}/task_id=${task}" -name '*.log' \
      -exec sh -c 'for f; do echo "### $f"; cat "$f"; done' _ {} + > "${dir}/airflow/${task}.log" 2>/dev/null || true
  done
  if [[ -f "${dir}/publication.json" ]]; then
    start_forward || die "no port-forward to the GKE Redis; collect again once it is reachable"
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
    # The tasks reach the GKE Redis through this invocation's forward; reopen it if kubectl dropped it.
    start_forward || echo "WARNING: port-forward unavailable; the Redis tasks will fail closed" >&2
    state=$(af_json dags list-runs "${DAG_ID}" | "${RAG_PY}" -c \
      "import json,sys;print(next((r['state'] for r in json.load(sys.stdin) if r['run_id']=='${run_id}'),'unknown'))" \
      2>/dev/null || echo unknown)
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
  [[ "${view}" == summary ]] || start_forward || die "no port-forward to the GKE Redis"
  py -m ops.llm_agent.rag_evidence "${view}" --run-dir "$(run_dir "${label}")"
}

forward() {
  [[ "${TARGET}" == gke ]] || die "forward is for RAG_TARGET=gke"
  start_forward || die "no port-forward"
  if (( ! FORWARD_OWNED )); then echo "port-forward already open (pid ${FORWARD_PID}, owned by another runner)"; return; fi
  echo "port-forward 127.0.0.1:${GKE_FORWARD_PORT} -> ${GKE_NAMESPACE}/svc/redis (pid ${FORWARD_PID}); Ctrl-C closes it"
  wait "${FORWARD_PID}" || true
}

stop() {
  "${AIRFLOW}" stop || true
  rm -f "${RAG_AIRFLOW_HOME}.target"
  stop_recorded_forward
  if docker ps --format '{{.Names}}' | grep -qx "${LOCAL_REDIS}"; then
    docker stop "${LOCAL_REDIS}" >/dev/null && echo "stopped ${LOCAL_REDIS}"
  fi
  if [[ "${1:-}" == --with-datahub ]]; then
    ./data_platform/metadata/datahub/runtime/start_datahub.sh stop   # containers only; volumes are kept
  fi
}

main() {
  trap close_forward EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  case "${1:-}" in
    preflight) preflight ;;
    prepare) prepare ;;
    start) start ;;
    trigger) trigger "${2:?RUN_LABEL}" ;;
    collect) collect "${2:?RUN_LABEL}" ;;
    show) show "${2:-}" "${3:-}" ;;
    stop) cd "${ROOT}" && stop "${2:-}" ;;
    dbs) dbs ;;
    configure) configure "${2:-}" "${3:-}" "${4:-}" ;;
    forward) forward ;;
    *) sed -n '2,26p' "$0"; exit 2 ;;
  esac
}

# Sourcing (tests) defines the functions only.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
