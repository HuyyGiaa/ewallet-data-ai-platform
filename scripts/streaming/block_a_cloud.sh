#!/usr/bin/env bash
# Run the Block A dual-store verification on GKE. Dry-run by default.
#
#   scripts/streaming/block_a_cloud.sh render    <verify-namespace>
#   scripts/streaming/block_a_cloud.sh preflight <verify-namespace> [--execute]
#   scripts/streaming/block_a_cloud.sh deploy   <verify-namespace> [--execute]
#   scripts/streaming/block_a_cloud.sh run      <verify-namespace> [--execute]
#   scripts/streaming/block_a_cloud.sh collect  <verify-namespace> [--execute]
#   scripts/streaming/block_a_cloud.sh cleanup  <verify-namespace> [--execute]
#
# Required: FLINK_IMAGE, SINKS_IMAGE (both pinned by digest), ONLINE_REGISTRY_URI,
# FEAST_OFFLINE_DATA_ROOT, OFFLINE_FEATURE_ROOT, HISTORY_ROOT (dedicated gs:// prefixes),
# FEAST_REDIS_SECRET. Secrets are referenced by name and never printed.
source "$(dirname "${BASH_SOURCE[0]}")/../mlops/lib/common.sh"

TEMPLATE_DIR="${REPO_ROOT}/infra/kubernetes/streaming-block-a"
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${EXPECTED_PROJECT_ID}_asia-southeast1-b_ewallet-dev}"
BROKER="block-a-redpanda.ml-platform.svc.cluster.local:29092"
PROFILE="${BLOCK_A_PROFILE:-baseline}"
EVIDENCE_ROOT="${BLOCK_A_EVIDENCE_ROOT:-$HOME/ewallet-evidence/c2-block-a}"
PREFLIGHT_ROOT="${BLOCK_A_PREFLIGHT_ROOT:-${EVIDENCE_ROOT}/preflight}"
ONLINE_REPO=ai_platform/features/feast/online_repo
CLI=data_platform.streaming.verification.cli
STACK_RESOURCES=(
  service/block-a-redpanda
  deployment/block-a-redpanda
  configmap/block-a-flink
  service/block-a-flink-jobmanager
  deployment/block-a-flink-jobmanager
  deployment/block-a-flink-taskmanager
)
JOB_RESOURCES=(
  job/block-a-topics
  job/block-a-history
  job/block-a-offline
  job/block-a-feast
  job/block-a-publish
  job/block-a-validate
)

kube() { kubectl --context "${KUBE_CONTEXT}" -n ml-platform "$@"; }
# redis_cli <db> <args...>: authenticate with the password already inside the Redis
# pod (requirepass), so no credential crosses this machine or reaches a log.
redis_cli() {
  local database="$1"
  shift
  kubectl --context "${KUBE_CONTEXT}" -n feature-store exec statefulset/redis -c redis -- \
    sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" exec redis-cli --no-auth-warning -n "$0" "$@"' "${database}" "$@"
}
# The Secret name and approved Redis databases come from the Python binding table.
binding_field() {
  python3 -m ai_platform.features.feast.materialization_plan "${ONLINE_STORE_ID}" \
    | awk -v field="$1" '{print $field}'
}
secret_database() {
  local connection database
  connection="$(kube get secret "${FEAST_REDIS_SECRET}" -o jsonpath='{.data.connection_string}' | base64 -d)"
  [[ "${connection#redis://}" == redis.feature-store.svc.cluster.local:6379,* ]] \
    || die "verification Secret points to an unapproved Redis host or port"
  database="$(tr ',' '\n' <<<"${connection}" | awk -F= '$1 == "db" {print $2}')"
  [[ "${database}" =~ ^[0-9]+$ ]] || die "verification Secret must select a Redis database"
  [[ ",$(binding_field 2)," == *",${database},"* ]] \
    || die "Redis DB ${database} is outside the ${ONLINE_STORE_ID} binding"
  printf '%s' "${database}"
}
apply_stdin() { kubectl --context "${KUBE_CONTEXT}" apply -f -; }

require_inputs() {
  local name
  for name in FLINK_IMAGE SINKS_IMAGE FLINK_IMAGE_SOURCE_SHA SINKS_IMAGE_SOURCE_SHA APPROVED_SHA ONLINE_REGISTRY_URI ONLINE_STORE_ID FEAST_OFFLINE_DATA_ROOT OFFLINE_FEATURE_ROOT HISTORY_ROOT FEAST_REDIS_SECRET DATASET_VERSION FEATURE_RUN_ID FEAST_SNAPSHOT_ID BLOCK_A_ESTIMATED_REDIS_BYTES; do
    require_env "${name}"
  done
  require_digest_pinned_images FLINK_IMAGE SINKS_IMAGE
  require_matching_source_shas "${APPROVED_SHA}" FLINK_IMAGE_SOURCE_SHA SINKS_IMAGE_SOURCE_SHA
  [[ "$(git -C "${REPO_ROOT}" rev-parse HEAD)" == "${APPROVED_SHA}" ]] \
    || die "local HEAD does not match APPROVED_SHA"
  [[ "${HISTORY_ROOT}" == gs://* && "${HISTORY_ROOT}" != */lakehouse/runs/* ]] \
    || die "HISTORY_ROOT must be a dedicated gs:// prefix"
  [[ "${OFFLINE_FEATURE_ROOT}" == gs://* ]] \
    || die "OFFLINE_FEATURE_ROOT must be a gs:// prefix"
  [[ "${ONLINE_REGISTRY_URI}" == */online/*/registry.db ]] || die "ONLINE_REGISTRY_URI is not an online registry"
  [[ "${DATASET_VERSION}" =~ ^[a-z0-9][a-z0-9._-]{2,79}$ ]] || die "invalid DATASET_VERSION"
  [[ "${FEATURE_RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{2,79}$ ]] || die "invalid FEATURE_RUN_ID"
  [[ "${FEAST_SNAPSHOT_ID}" =~ ^[a-z0-9][a-z0-9._-]{2,79}$ ]] || die "invalid FEAST_SNAPSHOT_ID"
  case "${DATASET_VERSION}:${ONLINE_STORE_ID}" in
    canonical-100gb-v1:canonical-verify|rehearsal-5gb-v1:rehearsal-stream) ;;
    *) die "dataset and verification Online Store binding are incompatible" ;;
  esac
  [[ "${FEAST_REDIS_SECRET}" == "$(binding_field 1)" ]] \
    || die "FEAST_REDIS_SECRET must be $(binding_field 1) for ${ONLINE_STORE_ID}"
}

require_namespace() {
  [[ "${1:-}" =~ ^verify-[a-z0-9][a-z0-9-]{0,46}$ ]] || die "invalid verification namespace: ${1:-}"
}

require_storage_binding() {
  local namespace="$1"
  python3 -m data_platform.config.dataset_identity validate-feature-store \
    --dataset-version "${DATASET_VERSION}" \
    --offline-feature-root "${OFFLINE_FEATURE_ROOT}" \
    --feast-offline-data-root "${FEAST_OFFLINE_DATA_ROOT}" \
    --history-root "${HISTORY_ROOT}" \
    --online-registry-uri "${ONLINE_REGISTRY_URI}" \
    --campaign-id "${namespace}" \
    --run-id "${namespace}" \
    || die "Feature Store storage binding is invalid"
}

assert_campaign_ownership() {
  local namespace="$1" resource observed owner
  shift
  for resource in "$@"; do
    if ! observed="$(kube get "${resource}" --ignore-not-found \
      -o 'go-template={{.metadata.name}}|{{index .metadata.labels "ewallet.block-a/namespace"}}')"; then
      die "could not inspect Kubernetes ownership for ${resource}"
    fi
    [[ -n "${observed}" ]] || continue
    [[ "${observed}" == *"|"* ]] || die "malformed Kubernetes ownership for ${resource}"
    owner="${observed#*|}"
    [[ -n "${owner}" ]] || die "existing ${resource} has no Block A campaign owner"
    [[ "${owner}" == "${namespace}" ]] \
      || die "existing ${resource} belongs to foreign campaign ${owner}"
  done
}

require_resources_absent() {
  local resource observed
  for resource in "$@"; do
    if ! observed="$(kube get "${resource}" --ignore-not-found -o name)"; then
      die "could not inspect Kubernetes resource ${resource}"
    fi
    [[ -z "${observed}" ]] \
      || die "${resource} already exists; preserve evidence and run matching-campaign cleanup before retry"
  done
}

require_resources_present() {
  local resource observed
  for resource in "$@"; do
    if ! observed="$(kube get "${resource}" --ignore-not-found -o name)"; then
      die "could not inspect required Kubernetes resource ${resource}"
    fi
    [[ -n "${observed}" ]] || die "required Kubernetes resource ${resource} is absent"
  done
}

render_stack() {
  FLINK_IMAGE="${FLINK_IMAGE}" BLOCK_A_NAMESPACE="$1" \
    envsubst '${FLINK_IMAGE} ${BLOCK_A_NAMESPACE}' <"${TEMPLATE_DIR}/block-a.yaml.tmpl"
}

# render_step <job-name> <namespace> <comma-joined quoted args>
render_step() {
  STEP_NAME="$1" BLOCK_A_NAMESPACE="$2" STEP_ARGS="$3" GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)" \
    SINKS_IMAGE="${SINKS_IMAGE}" ONLINE_REGISTRY_URI="${ONLINE_REGISTRY_URI}" \
    FEAST_OFFLINE_DATA_ROOT="${FEAST_OFFLINE_DATA_ROOT}" FEAST_REDIS_SECRET="${FEAST_REDIS_SECRET}" \
    OFFLINE_FEATURE_ROOT="${OFFLINE_FEATURE_ROOT}" HISTORY_ROOT="${HISTORY_ROOT}" \
    ONLINE_STORE_ID="${ONLINE_STORE_ID}" \
    BLOCK_A_ESTIMATED_REDIS_BYTES="${BLOCK_A_ESTIMATED_REDIS_BYTES}" \
    DATASET_VERSION="${DATASET_VERSION}" FEATURE_RUN_ID="${FEATURE_RUN_ID}" FEAST_SNAPSHOT_ID="${FEAST_SNAPSHOT_ID}" \
    envsubst '${STEP_NAME} ${BLOCK_A_NAMESPACE} ${STEP_ARGS} ${GIT_SHA} ${SINKS_IMAGE} ${ONLINE_REGISTRY_URI} ${ONLINE_STORE_ID} ${FEAST_OFFLINE_DATA_ROOT} ${OFFLINE_FEATURE_ROOT} ${HISTORY_ROOT} ${FEAST_REDIS_SECRET} ${DATASET_VERSION} ${FEATURE_RUN_ID} ${FEAST_SNAPSHOT_ID} ${BLOCK_A_ESTIMATED_REDIS_BYTES}' \
    <"${TEMPLATE_DIR}/sink-job.yaml.tmpl"
}

quoted() {
  local result="" item
  for item in "$@"; do
    result+="${result:+, }\"${item}\""
  done
  printf '%s' "${result}"
}

topics_args() {
  quoted python -m "${CLI}" publish --bootstrap-servers "${BROKER}" --namespace "$1" \
    --profile "${PROFILE}" --manifest /tmp/manifest.json --create-topics --create-topics-only
}

history_args() {
  quoted python -m data_platform.streaming.sinks.historical_service --bootstrap-servers "${BROKER}" \
    --namespace "$1" --run-id "$1" --history-root "${HISTORY_ROOT}" --batch-size 5 \
    --flush-interval-seconds 5 --max-idle-seconds 240
}

offline_args() {
  quoted python -m data_platform.streaming.sinks.offline_features --bootstrap-servers "${BROKER}" \
    --namespace "$1" --campaign-id "$1" --run-id "$1" \
    --offline-root "${OFFLINE_FEATURE_ROOT}" --max-idle-seconds 240
}

feast_args() {
  quoted python -m data_platform.streaming.feast.service --bootstrap-servers "${BROKER}" \
    --namespace "$1" --feast-repo "${ONLINE_REPO}" --max-idle-seconds 240
}

publish_args() {
  quoted python -m "${CLI}" publish --bootstrap-servers "${BROKER}" --namespace "$1" \
    --profile "${PROFILE}" --manifest /tmp/manifest.json --phase-delay-seconds 15
}

# The manifest is rebuilt from the namespace, so the validator needs no shared volume.
validate_args() {
  quoted sh -c "python -m ${CLI} publish --bootstrap-servers ${BROKER} --namespace $1 --profile ${PROFILE} --manifest /tmp/manifest.json --create-topics-only && python -m ${CLI} validate --bootstrap-servers ${BROKER} --namespace $1 --manifest /tmp/manifest.json --history-root ${HISTORY_ROOT} --offline-feature-root ${FEAST_OFFLINE_DATA_ROOT} --campaign-id $1 --run-id $1 --feast-repo ${ONLINE_REPO} --report /tmp/report.json >/tmp/validator-summary.json; status=\$?; cat /tmp/validator-summary.json >&2; if [ -f /tmp/report.json ]; then printf 'EWALLET_CONSISTENCY_REPORT='; tr -d '\n' </tmp/report.json; printf '\n'; fi; exit \$status"
}

configuration_fingerprint() {
  local namespace="$1"
  printf '%s\0' "${namespace}" "$(git -C "${REPO_ROOT}" rev-parse HEAD)" \
    "${FLINK_IMAGE}" "${SINKS_IMAGE}" "${ONLINE_REGISTRY_URI}" \
    "${FEAST_OFFLINE_DATA_ROOT}" "${OFFLINE_FEATURE_ROOT}" "${HISTORY_ROOT}" \
    "${FEAST_REDIS_SECRET}" "${ONLINE_STORE_ID}" "${DATASET_VERSION}" "${FEATURE_RUN_ID}" \
    "${FEAST_SNAPSHOT_ID}" "${KUBE_CONTEXT}" "${BLOCK_A_ESTIMATED_REDIS_BYTES}" \
    "${APPROVED_SHA}" "${FLINK_IMAGE_SOURCE_SHA}" "${SINKS_IMAGE_SOURCE_SHA}" \
    | sha256sum | awk '{print $1}'
}

preflight_artifact() { printf '%s/%s.preflight' "${PREFLIGHT_ROOT}" "$1"; }

require_preflight() {
  local namespace="$1" artifact fingerprint created now expected_resource_version current_resource_version
  local info server maxmemory used policy projected run_id current_target_fingerprint
  local current_owner current_owner_sha256 expected_owner_sha256 database
  artifact="$(preflight_artifact "${namespace}")"
  [[ -f "${artifact}" ]] || die "missing successful preflight for ${namespace}"
  fingerprint="$(configuration_fingerprint "${namespace}")"
  grep -Fxq 'status=PASS' "${artifact}" || die "preflight did not pass"
  grep -Fxq "namespace=${namespace}" "${artifact}" || die "preflight campaign mismatch"
  grep -Fxq "configuration_fingerprint=${fingerprint}" "${artifact}" \
    || die "preflight configuration is stale or mismatched"
  created="$(awk -F= '$1 == "created_epoch" {print $2}' "${artifact}")"
  [[ "${created}" =~ ^[0-9]+$ ]] || die "preflight timestamp is invalid"
  now="$(date +%s)"
  ((now - created <= 3600)) || die "preflight is older than one hour"
  expected_resource_version="$(awk -F= '$1 == "redis_secret_resource_version" {print $2}' "${artifact}")"
  current_resource_version="$(kube get secret "${FEAST_REDIS_SECRET}" -o jsonpath='{.metadata.resourceVersion}')"
  [[ -n "${expected_resource_version}" && "${current_resource_version}" == "${expected_resource_version}" ]] \
    || die "Redis Secret changed after preflight"
  database="$(secret_database)"
  grep -Fxq "redis_db=${database}" "${artifact}" || die "Redis database changed after preflight"
  info="$(redis_cli "${database}" INFO memory)"
  server="$(redis_cli "${database}" INFO server)"
  maxmemory="$(awk -F: '$1 == "maxmemory" {gsub("\r", "", $2); print $2}' <<<"${info}")"
  used="$(awk -F: '$1 == "used_memory" {gsub("\r", "", $2); print $2}' <<<"${info}")"
  policy="$(awk -F: '$1 == "maxmemory_policy" {gsub("\r", "", $2); print $2}' <<<"${info}")"
  run_id="$(awk -F: '$1 == "run_id" {gsub("\r", "", $2); print $2}' <<<"${server}")"
  [[ "${maxmemory}" =~ ^[1-9][0-9]*$ && "${used}" =~ ^[0-9]+$ && -n "${run_id}" ]] \
    || die "current Redis capacity or server identity is invalid"
  [[ "${policy}" == noeviction ]] || die "current Redis maxmemory-policy is not noeviction"
  projected=$((used + BLOCK_A_ESTIMATED_REDIS_BYTES))
  ((projected * 10 <= maxmemory * 9)) || die "current projected Redis use exceeds the 90% safety ceiling"
  current_target_fingerprint="sha256:$(printf '%s' "redis.feature-store.svc.cluster.local:6379/${database}:${run_id}" | sha256sum | awk '{print $1}')"
  grep -Fxq "target_fingerprint=${current_target_fingerprint}" "${artifact}" \
    || die "physical Redis target changed after preflight"
  current_owner="$(redis_cli "${database}" GET __ewallet_feature_store_campaign_owner_v1__)"
  current_owner_sha256="$(printf '%s' "${current_owner}" | sha256sum | awk '{print $1}')"
  expected_owner_sha256="$(awk -F= '$1 == "redis_owner_sha256" {print $2}' "${artifact}")"
  [[ -n "${current_owner}" && "${current_owner_sha256}" == "${expected_owner_sha256}" ]] \
    || die "Redis campaign ownership changed after preflight"
}

cmd_render() {
  require_inputs
  local namespace="${1:-}" step
  require_namespace "${namespace}"
  require_storage_binding "${namespace}"
  render_stack "${namespace}"
  for step in topics history offline feast publish validate; do
    printf -- '---\n'
    render_step "block-a-${step}" "${namespace}" "$("${step}_args" "${namespace}")"
  done
}

cmd_preflight() {
  require_inputs
  local namespace="${1:-}"
  require_namespace "${namespace}"
  require_storage_binding "${namespace}"
  require_env BLOCK_A_ESTIMATED_REDIS_BYTES
  [[ "${BLOCK_A_ESTIMATED_REDIS_BYTES}" =~ ^[1-9][0-9]*$ ]] \
    || die "BLOCK_A_ESTIMATED_REDIS_BYTES must be a positive integer"
  if ((!EXECUTE)); then
    print_command kubectl --context "${KUBE_CONTEXT}" -n ml-platform get secret "${FEAST_REDIS_SECRET}" -o name
    print_command kubectl --context "${KUBE_CONTEXT}" -n feature-store exec statefulset/redis -c redis -- redis-cli INFO memory
    echo "  assert the verification Secret exists, points to a non-production Redis DB, maxmemory-policy=noeviction, and projected use stays below 90%"
    return
  fi
  ((APPROVE_REDIS_CLAIM)) \
    || die "preflight --execute mutates Redis ownership; explicit --approve-redis-claim is required"
  local info server maxmemory used policy projected run_id target_fingerprint owner claim_status resource_version database
  database="$(secret_database)"
  resource_version="$(kube get secret "${FEAST_REDIS_SECRET}" -o jsonpath='{.metadata.resourceVersion}')"
  [[ -n "${resource_version}" ]] || die "verification Secret has no resource version"
  info="$(redis_cli "${database}" INFO memory)"
  server="$(redis_cli "${database}" INFO server)"
  maxmemory="$(awk -F: '$1 == "maxmemory" {gsub("\\r", "", $2); print $2}' <<<"${info}")"
  used="$(awk -F: '$1 == "used_memory" {gsub("\\r", "", $2); print $2}' <<<"${info}")"
  policy="$(awk -F: '$1 == "maxmemory_policy" {gsub("\\r", "", $2); print $2}' <<<"${info}")"
  [[ "${maxmemory}" =~ ^[1-9][0-9]*$ && "${used}" =~ ^[0-9]+$ ]] || die "could not read bounded Redis memory settings"
  [[ "${policy}" == noeviction ]] || die "Redis maxmemory-policy must be noeviction"
  projected=$((used + BLOCK_A_ESTIMATED_REDIS_BYTES))
  ((projected * 10 <= maxmemory * 9)) || die "projected Redis use exceeds the 90% safety ceiling"
  run_id="$(awk -F: '$1 == "run_id" {gsub("\r", "", $2); print $2}' <<<"${server}")"
  [[ -n "${run_id}" ]] || die "could not identify the Redis server run"
  target_fingerprint="sha256:$(printf '%s' "redis.feature-store.svc.cluster.local:6379/${database}:${run_id}" | sha256sum | awk '{print $1}')"
  owner="{\"campaign_id\":\"${namespace}\",\"dataset_version\":\"${DATASET_VERSION}\",\"online_store_id\":\"${ONLINE_STORE_ID}\",\"registry_uri\":\"${ONLINE_REGISTRY_URI}\",\"schema_version\":\"ewallet-redis-campaign-owner-v1\",\"snapshot_id\":\"${FEAST_SNAPSHOT_ID}\",\"source_run_id\":\"${FEATURE_RUN_ID}\",\"target_fingerprint\":\"${target_fingerprint}\"}"
  claim_status="$(redis_cli "${database}" EVAL "local c=redis.call('GET',KEYS[1]); if c then if c==ARGV[1] then return 'OWNED' else return 'CONFLICT' end end; if redis.call('DBSIZE')~=0 then return 'AMBIGUOUS_NONEMPTY' end; redis.call('SET',KEYS[1],ARGV[1]); return 'CLAIMED'" 1 __ewallet_feature_store_campaign_owner_v1__ "${owner}")"
  [[ "${claim_status}" == CLAIMED || "${claim_status}" == OWNED ]] \
    || die "verification Redis ownership failed: ${claim_status}"
  mkdir -p "${PREFLIGHT_ROOT}"
  local artifact temporary
  artifact="$(preflight_artifact "${namespace}")"
  temporary="$(mktemp "${PREFLIGHT_ROOT}/.${namespace}.XXXXXX")"
  printf 'status=PASS\nnamespace=%s\nconfiguration_fingerprint=%s\ntarget_fingerprint=%s\nredis_db=%s\nredis_secret_resource_version=%s\nredis_owner_sha256=%s\ncreated_epoch=%s\n' \
    "${namespace}" "$(configuration_fingerprint "${namespace}")" "${target_fingerprint}" "${database}" \
    "${resource_version}" "$(printf '%s' "${owner}" | sha256sum | awk '{print $1}')" \
    "$(date +%s)" >"${temporary}"
  mv "${temporary}" "${artifact}"
  echo "preflight=PASS namespace=${namespace} redis_db=${database} claim=${claim_status} target_fingerprint=${target_fingerprint} artifact=${artifact}"
}

cmd_deploy() {
  require_inputs
  local namespace="${1:-}"
  require_namespace "${namespace}"
  require_storage_binding "${namespace}"
  if ((!EXECUTE)); then
    render_stack "${namespace}"
    echo "DRY RUN: nothing applied; rerun with --execute" >&2
    return
  fi
  require_preflight "${namespace}"
  assert_campaign_ownership "${namespace}" "${STACK_RESOURCES[@]}"
  render_stack "${namespace}" | apply_stdin
  kube rollout status deployment/block-a-redpanda --timeout=300s
  kube rollout status deployment/block-a-flink-jobmanager --timeout=300s
  kube rollout status deployment/block-a-flink-taskmanager --timeout=300s
}

cmd_run() {
  require_inputs
  local namespace="${1:-}" job step
  require_namespace "${namespace}"
  require_storage_binding "${namespace}"
  if ((EXECUTE)); then require_preflight "${namespace}"; fi
  local expected_offline_root="${OFFLINE_FEATURE_ROOT%/}/campaigns/${namespace}/runs/${namespace}/offline"
  [[ "${FEAST_OFFLINE_DATA_ROOT%/}" == "${expected_offline_root}" ]] \
    || die "FEAST_OFFLINE_DATA_ROOT must be ${expected_offline_root} for this namespace"
  if ((!EXECUTE)); then
    echo "DRY RUN: would create topics, submit ISOLATED_VERIFICATION, start the sinks and publish ${namespace}"
    return
  fi
  assert_campaign_ownership "${namespace}" "${STACK_RESOURCES[@]}" "${JOB_RESOURCES[@]}"
  require_resources_present "${STACK_RESOURCES[@]}"
  require_resources_absent "${JOB_RESOURCES[@]}"
  render_step block-a-topics "${namespace}" "$(topics_args "${namespace}")" | apply_stdin
  kube wait --for=condition=complete --timeout=180s job/block-a-topics
  job=$(kube exec deploy/block-a-flink-jobmanager -- flink run -d \
    -py /opt/ewallet/data_platform/streaming/flink/job.py --mode ISOLATED_VERIFICATION \
    --profile "${PROFILE}" --namespace "${namespace}" --bootstrap-servers "${BROKER}" \
    --checkpoint-storage "file:///flink-data/checkpoints/${namespace}" | grep -oE 'JobID [0-9a-f]{32}' | awk '{print $2}' | tail -1)
  [[ -n "${job}" ]] || die "Flink returned no JobID"
  echo "flink_job_id=${job}"
  for step in history offline feast publish; do
    render_step "block-a-${step}" "${namespace}" "$("${step}_args" "${namespace}")" | apply_stdin
  done
  kube wait --for=condition=complete --timeout=300s job/block-a-publish
  kube wait --for=condition=complete --timeout=600s job/block-a-history job/block-a-offline job/block-a-feast
}

cmd_collect() {
  require_inputs
  local namespace="${1:-}" run step evidence_failed=0 validation_failed=0
  local raw_report report_json
  require_namespace "${namespace}"
  require_storage_binding "${namespace}"
  if ((EXECUTE)); then require_preflight "${namespace}"; fi
  run="${EVIDENCE_ROOT}/${namespace}"
  if ((!EXECUTE)); then
    echo "DRY RUN: would collect job logs, Flink REST state and the consistency report into ${run}"
    return
  fi
  assert_campaign_ownership "${namespace}" "${STACK_RESOURCES[@]}" job/block-a-validate
  require_resources_present "${STACK_RESOURCES[@]}"
  require_resources_absent job/block-a-validate
  mkdir -p "${run}"
  for step in topics publish history offline feast; do
    if ! kube logs "job/block-a-${step}" >"${run}/block-a-${step}.log" 2>&1; then
      evidence_failed=1
    fi
  done
  if ! kube exec deploy/block-a-flink-jobmanager -- python -c \
    "import urllib.request as u; print(u.urlopen('http://localhost:8081/jobs/overview').read().decode())" \
    >"${run}/flink-jobs.json" 2>"${run}/flink-jobs.error.log"; then
    evidence_failed=1
  fi
  render_step block-a-validate "${namespace}" "$(validate_args "${namespace}")" | apply_stdin
  if ! kube wait --for=condition=complete --timeout=300s job/block-a-validate; then
    echo "validator Job failed or timed out; preserving available evidence" >&2
    validation_failed=1
  fi
  if ! kube get job/block-a-validate -o json >"${run}/validator-job.json" \
    2>"${run}/validator-job.error.log"; then
    evidence_failed=1
  fi
  raw_report="${run}/consistency-report.log"
  report_json="${run}/consistency-report.json"
  if ! kube logs job/block-a-validate >"${raw_report}" 2>&1; then
    validation_failed=1
  fi
  sed -n 's/^EWALLET_CONSISTENCY_REPORT=//p' "${raw_report}" | tail -1 >"${report_json}"
  if [[ ! -s "${report_json}" ]]; then
    echo "validator produced no machine-readable consistency report" >&2
    validation_failed=1
  elif ! python3 -m "${CLI}" check-report --report "${report_json}" \
    >"${run}/consistency-verdict.json" 2>"${run}/consistency-verdict.error.log"; then
    echo "validator consistency report failed its contract" >&2
    validation_failed=1
  fi
  if ((validation_failed || evidence_failed)); then
    return 1
  fi
  echo "verification=PASS report=${report_json}"
}

cmd_cleanup() {
  require_inputs
  local namespace="${1:-}"
  require_namespace "${namespace}"
  require_storage_binding "${namespace}"
  if ((!EXECUTE)); then
    echo "DRY RUN: would delete only ${namespace} objects carrying both Block A campaign labels"
    return
  fi
  require_preflight "${namespace}"
  assert_campaign_ownership "${namespace}" "${STACK_RESOURCES[@]}" "${JOB_RESOURCES[@]}"
  kube delete job,deployment,service,configmap \
    -l "app.kubernetes.io/part-of=ewallet-block-a,ewallet.block-a/namespace=${namespace}"
}

command="${1:-}"
[[ -n "${command}" ]] || die "usage: $0 render|preflight|deploy|run|collect|cleanup"
shift
positional=()
EXECUTE=0
APPROVE_REDIS_CLAIM=0
for argument in "$@"; do
  case "${argument}" in
    --execute) EXECUTE=1 ;;
    --approve-redis-claim) APPROVE_REDIS_CLAIM=1 ;;
    *) positional+=("${argument}") ;;
  esac
done
if ((APPROVE_REDIS_CLAIM)) && [[ "${command}" != preflight ]]; then
  die "--approve-redis-claim is valid only for preflight"
fi
[[ ${#positional[@]} -eq 1 ]] \
  || die "usage: $0 ${command} <verify-namespace> [--execute]"
if ((EXECUTE)) && [[ "${command}" == render ]]; then
  die "--execute is not valid for render; render is local and read-only"
fi
case "${command}" in
  render) cmd_render "${positional[@]}" ;;
  preflight) cmd_preflight "${positional[@]}" ;;
  deploy) cmd_deploy "${positional[@]}" ;;
  run) cmd_run "${positional[@]}" ;;
  collect) cmd_collect "${positional[@]}" ;;
  cleanup) cmd_cleanup "${positional[@]}" ;;
  *) die "unsupported command: ${command}" ;;
esac
