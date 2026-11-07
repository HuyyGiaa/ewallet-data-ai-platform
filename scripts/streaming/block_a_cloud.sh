#!/usr/bin/env bash
# Run the Block A dual-store verification on GKE. Dry-run by default.
#
#   scripts/streaming/block_a_cloud.sh render
#   scripts/streaming/block_a_cloud.sh deploy   [--execute]
#   scripts/streaming/block_a_cloud.sh run      <verify-namespace> [--execute]
#   scripts/streaming/block_a_cloud.sh collect  <verify-namespace> [--execute]
#   scripts/streaming/block_a_cloud.sh cleanup  [--execute]
#
# Required: FLINK_IMAGE, SINKS_IMAGE (both pinned by digest), ONLINE_REGISTRY_URI,
# FEAST_OFFLINE_DATA_ROOT, HISTORY_ROOT (gs://.../streaming-history/...),
# FEAST_REDIS_SECRET. Secrets are referenced by name and never printed.
source "$(dirname "${BASH_SOURCE[0]}")/../mlops/lib/common.sh"

TEMPLATE_DIR="${REPO_ROOT}/infra/kubernetes/streaming-block-a"
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${EXPECTED_PROJECT_ID}_asia-southeast1-b_ewallet-dev}"
BROKER="block-a-redpanda.ml-platform.svc.cluster.local:29092"
PROFILE="${BLOCK_A_PROFILE:-baseline}"
EVIDENCE_ROOT="${BLOCK_A_EVIDENCE_ROOT:-$HOME/ewallet-evidence/c2-block-a}"
ONLINE_REPO=ai_platform/features/feast/online_repo
CLI=data_platform.streaming.verification.cli

kube() { kubectl --context "${KUBE_CONTEXT}" -n ml-platform "$@"; }
apply_stdin() { kubectl --context "${KUBE_CONTEXT}" apply -f -; }

require_inputs() {
  local name
  for name in FLINK_IMAGE SINKS_IMAGE ONLINE_REGISTRY_URI FEAST_OFFLINE_DATA_ROOT HISTORY_ROOT FEAST_REDIS_SECRET; do
    require_env "${name}"
  done
  [[ "${FLINK_IMAGE}" == *@sha256:* ]] || die "FLINK_IMAGE must be pinned by digest"
  [[ "${SINKS_IMAGE}" == *@sha256:* ]] || die "SINKS_IMAGE must be pinned by digest"
  [[ "${HISTORY_ROOT}" == gs://* && "${HISTORY_ROOT}" != */lakehouse/runs/* ]] \
    || die "HISTORY_ROOT must be a dedicated gs:// prefix"
  [[ "${ONLINE_REGISTRY_URI}" == */online/*/registry.db ]] || die "ONLINE_REGISTRY_URI is not an online registry"
  [[ "${FEAST_REDIS_SECRET}" != feast-online-redis ]] \
    || die "use a Secret that selects a separate Redis database, so live keys are untouched"
}

require_namespace() {
  [[ "${1:-}" =~ ^verify-[a-z0-9][a-z0-9-]{0,46}$ ]] || die "invalid verification namespace: ${1:-}"
}

render_stack() {
  FLINK_IMAGE="${FLINK_IMAGE}" envsubst '${FLINK_IMAGE}' <"${TEMPLATE_DIR}/block-a.yaml.tmpl"
}

# render_step <job-name> <namespace> <comma-joined quoted args>
render_step() {
  STEP_NAME="$1" BLOCK_A_NAMESPACE="$2" STEP_ARGS="$3" GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)" \
    SINKS_IMAGE="${SINKS_IMAGE}" ONLINE_REGISTRY_URI="${ONLINE_REGISTRY_URI}" \
    FEAST_OFFLINE_DATA_ROOT="${FEAST_OFFLINE_DATA_ROOT}" FEAST_REDIS_SECRET="${FEAST_REDIS_SECRET}" \
    envsubst '${STEP_NAME} ${BLOCK_A_NAMESPACE} ${STEP_ARGS} ${GIT_SHA} ${SINKS_IMAGE} ${ONLINE_REGISTRY_URI} ${FEAST_OFFLINE_DATA_ROOT} ${FEAST_REDIS_SECRET}' \
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
  quoted sh -c "python -m ${CLI} publish --bootstrap-servers ${BROKER} --namespace $1 --profile ${PROFILE} --manifest /tmp/manifest.json --create-topics-only && python -m ${CLI} validate --bootstrap-servers ${BROKER} --namespace $1 --manifest /tmp/manifest.json --history-root ${HISTORY_ROOT} --run-id $1 --feast-repo ${ONLINE_REPO} --report /tmp/report.json; status=\$\$?; cat /tmp/report.json; exit \$\$status"
}

cmd_render() {
  require_inputs
  local namespace="verify-render-example" step
  render_stack
  for step in topics history feast publish validate; do
    printf -- '---\n'
    render_step "block-a-${step}" "${namespace}" "$("${step}_args" "${namespace}")"
  done
}

cmd_deploy() {
  require_inputs
  if ((!EXECUTE)); then
    render_stack
    echo "DRY RUN: nothing applied; rerun with --execute" >&2
    return
  fi
  render_stack | apply_stdin
  kube rollout status deployment/block-a-redpanda --timeout=300s
  kube rollout status deployment/block-a-flink-jobmanager --timeout=300s
  kube rollout status deployment/block-a-flink-taskmanager --timeout=300s
}

cmd_run() {
  require_inputs
  local namespace="${1:-}" job step
  require_namespace "${namespace}"
  if ((!EXECUTE)); then
    echo "DRY RUN: would create topics, submit ISOLATED_VERIFICATION, start the sinks and publish ${namespace}"
    return
  fi
  render_step block-a-topics "${namespace}" "$(topics_args "${namespace}")" | apply_stdin
  kube wait --for=condition=complete --timeout=180s job/block-a-topics
  job=$(kube exec deploy/block-a-flink-jobmanager -- flink run -d \
    -py /opt/ewallet/data_platform/streaming/flink/job.py --mode ISOLATED_VERIFICATION \
    --profile "${PROFILE}" --namespace "${namespace}" --bootstrap-servers "${BROKER}" \
    --checkpoint-storage "file:///flink-data/checkpoints/${namespace}" | grep -oE 'JobID [0-9a-f]{32}' | awk '{print $2}' | tail -1)
  [[ -n "${job}" ]] || die "Flink returned no JobID"
  echo "flink_job_id=${job}"
  for step in history feast publish; do
    render_step "block-a-${step}" "${namespace}" "$("${step}_args" "${namespace}")" | apply_stdin
  done
  kube wait --for=condition=complete --timeout=300s job/block-a-publish
  kube wait --for=condition=complete --timeout=600s job/block-a-history job/block-a-feast
}

cmd_collect() {
  require_inputs
  local namespace="${1:-}" run step
  require_namespace "${namespace}"
  run="${EVIDENCE_ROOT}/${namespace}"
  if ((!EXECUTE)); then
    echo "DRY RUN: would collect job logs, Flink REST state and the consistency report into ${run}"
    return
  fi
  mkdir -p "${run}"
  for step in topics publish history feast; do
    kube logs "job/block-a-${step}" >"${run}/block-a-${step}.log" 2>&1 || true
  done
  kube exec deploy/block-a-flink-jobmanager -- python -c \
    "import urllib.request as u; print(u.urlopen('http://localhost:8081/jobs/overview').read().decode())" >"${run}/flink-jobs.json"
  render_step block-a-validate "${namespace}" "$(validate_args "${namespace}")" | apply_stdin
  kube wait --for=condition=complete --timeout=300s job/block-a-validate \
    || echo "validator did not finish cleanly; read the report" >&2
  kube logs job/block-a-validate >"${run}/consistency-report.log"
}

cmd_cleanup() {
  if ((!EXECUTE)); then
    echo "DRY RUN: would delete every object labelled app.kubernetes.io/part-of=ewallet-block-a in ml-platform"
    return
  fi
  kube delete job,deployment,service,configmap -l app.kubernetes.io/part-of=ewallet-block-a
}

command="${1:-}"
[[ -n "${command}" ]] || die "usage: $0 render|deploy|run|collect|cleanup"
shift
positional=()
EXECUTE=0
for argument in "$@"; do
  if [[ "${argument}" == --execute ]]; then EXECUTE=1; else positional+=("${argument}"); fi
done
case "${command}" in
  render) cmd_render ;;
  deploy) cmd_deploy ;;
  run) cmd_run "${positional[@]:-}" ;;
  collect) cmd_collect "${positional[@]:-}" ;;
  cleanup) cmd_cleanup ;;
  *) die "unsupported command: ${command}" ;;
esac
