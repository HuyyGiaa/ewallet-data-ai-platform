#!/usr/bin/env bash
# Fail-closed gates of the agent-integration (AGI) evidence campaign.
#
#   agi_campaign.sh preflight                [READ-ONLY]         Stage 0; writes stage0/PREFLIGHT_PASS only if every check passes
#   agi_campaign.sh gate mcp|agents          [READ-ONLY]         render, exact object-identity diff, server-side dry-run
#   agi_campaign.sh upgrade mcp|agents       [MUTATES, needs A1] re-runs the gate, then helm upgrade --atomic
#   agi_campaign.sh negative                 [READ-ONLY]         AGI-08 negative probes; each must observe its expected denial
#   agi_campaign.sh data-state before|after  [READ-ONLY]         Redis key counts, owners, DB 3 digest, bounded DB 0 readback
#   agi_campaign.sh rollback mcp|agents      [MUTATES, needs A2] helm rollback to the preflight revision, then verify
#
# Configuration: AGI_ENV names a non-secret campaign file (see the runbook, section 0) that defines
# CAMPAIGN, EV, CTX, SHA and, per stage, image digests, pins and API-server CIDRs. Approvals are given per
# campaign: AGI_APPROVAL=A1:<campaign> for upgrades, AGI_APPROVAL=A2:<campaign> for rollbacks.
#
# AGI_PACKAGE selects one agent-platform package per preflight/gate/upgrade/rollback (default agi, the
# AGI overlays alone). Each has its own release, overlay, exact expected identities and approval
# (AGI_APPROVAL=<PACKAGE>:<campaign>, rollback <PACKAGE>-ROLLBACK:<campaign>); run preflight again
# before each package, so its live snapshot already contains the packages applied before it:
#   p1-mcp     llm-agent-mcp  AGI MCP overlay + agi/mcp-reliability.yaml (2 replicas, PDBs, spread)
#   s1-mcp     llm-agent-mcp  agi/mcp-sandbox-grants.yaml + mcpCredentialLifecycle.serverSet=SANDBOX_SERVER_SET
#   s1-agents  llm-agent      the live crossNamespace.flows + the sandbox-workers-to-mcp-gateway ingress flow
#   p2-agents  llm-agent      AGI agents overlay + g3/keda.yaml + agi/keda.yaml (KEDA adopts the HPAs);
#                             its rollback deletes the three ScaledObjects first, then rolls back
#   p4-agents  llm-agent      Coordinator sandbox routes (a2aGateway.sandboxRoutes, sandboxSpecialists)
#   t1-agents  llm-agent      the live crossNamespace.flows + sandbox-workers-to-otel-collector (OTLP 4317 only)
#   tr-agents  llm-agent      the live crossNamespace.flows + kagent-controller-to-otel-collector (OTLP 4317 only;
#                             P2-C network step, before the kagent controller gets its OTLP endpoint)
#   img-mcp    llm-agent-mcp  image roll: the live values with MCP_IMAGE and its identity, nothing else
#   img-agents llm-agent      image roll: the live values with the three BYO agent images, nothing else
#   img-coordinator llm-agent image roll of the Coordinator only (COORDINATOR_IMAGE); the Specialists keep
#                             their live images (P2-D: a Coordinator-only metrics change)
#                             (both after P2; a plain rollback restores the previous images)
# Every command exits non-zero on the first failed check; a failed check never reaches a mutating command.
# Checks and render arguments run in a fresh bash process (__check, __args): bash ignores `set -e` inside a
# function called from an if/||/&& context, which would let a failing step report PASS.
set -euo pipefail
shopt -s inherit_errexit

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHART="${ROOT}/infra/helm/llm-agent-platform"
HELM="${HELM:-helm}"
KUBECTL="${KUBECTL:-kubectl}"
GCLOUD="${GCLOUD:-gcloud}"
PYTHON="${PYTHON:-python3}"
RAG_RUNNER="${AGI_RAG_RUNNER:-${ROOT}/scripts/llm_agent/rag_runtime_run.sh}"
read -r -a AGENT_PY <<< "${AGI_AGENT_PY:-uv run --frozen --project ${ROOT}/infra/docker/g3-byo-coordinator python}"
DEFAULT_SNAPSHOT_MANIFEST_SHA256=3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca
GIT_DIR_CHECKED="${AGI_GIT_DIR:-${ROOT}}"
SNAPSHOT_URI=gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/features/offline/snapshots/rehearsal-5gb-v1/ab-20261110225456-optimized-feat-r261110225459/feast-r5gb-20261111-214544
RAG_PROJECT=ewallet_rag_hybrid_20261114t144825
REFERENCE_INPUT="${ROOT}/docs/evidence/llm_agent/agent_platform_runtime_20261102T104744Z/rollback/business-input.json"
FEATURES='["account_behavior__account_tx_count_1h","account_behavior__account_tx_count_24h","user_behavior__user_avg_amount_30d","merchant_behavior__merchant_tx_count_24h"]'

die() { echo "FAIL: $*" >&2; exit 1; }
PACKAGES=" agi p1-mcp s1-mcp s1-agents p2-agents p4-agents img-mcp img-agents img-coordinator t1-agents tr-agents "
SANDBOX_SPECIALISTS="{drift-specialist,feature-rag-specialist}"
KEDA_AGENTS="fraud-coordinator feature-rag-specialist drift-specialist"
# What the MCP and BYO images copy (infra/docker/*/Dockerfile). IMAGE_SHA may precede SHA only when
# none of these differ, so the identity the pods report is the source they were built from.
IMAGE_INPUTS=(ai_platform data/llm_agent ops/llm_agent infra/docker/llm-agent-tooling infra/docker/g3-byo-coordinator)
need() { local name; for name in "$@"; do [[ -n "${!name:-}" ]] || die "${name} is not set (campaign file ${AGI_ENV:-unset})"; done; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "${EV}/campaign.log"; }

load() {
  [[ -n "${AGI_ENV:-}" && -r "${AGI_ENV}" ]] || die "AGI_ENV must name the readable campaign file"
  # shellcheck disable=SC1090
  source "${AGI_ENV}"
  need CAMPAIGN EV CTX SHA
  PACKAGE="${AGI_PACKAGE:-agi}"
  [[ " ${PACKAGES} " == *" ${PACKAGE} "* ]] || die "AGI_PACKAGE must be one of:${PACKAGES}"
  SNAPSHOT_MANIFEST_SHA256="${SNAPSHOT_MANIFEST_SHA256:-${DEFAULT_SNAPSHOT_MANIFEST_SHA256}}"
  [[ "${CAMPAIGN}" =~ ^agi-[0-9]{8}T[0-9]{6}Z$ ]] || die "CAMPAIGN must look like agi-YYYYMMDDTHHMMSSZ"
  [[ "${SHA}" =~ ^[0-9a-f]{40}$ ]] || die "SHA must be a full commit SHA"
  mkdir -p "${EV}"/{stage0,stage1,stage2,stage3,stage4,stage5,inputs}
  PRIVATE="${AGI_PRIVATE:-/tmp/agi-${CAMPAIGN}}"   # helm values and manifests: never committed
  mkdir -p "${PRIVATE}" && chmod 700 "${PRIVATE}"
}

kc() { "${KUBECTL}" --context "${CTX}" "$@"; }
revision_key() { echo "${1//-/_}_REVISION" | tr 'a-z' 'A-Z'; }
hl() { "${HELM}" --kube-context "${CTX}" "$@"; }

package_release() {   # the release a package changes; agi changes both, one per call
  case "${PACKAGE}" in
    agi) echo "$1" ;;
    p1-mcp|s1-mcp|img-mcp) echo mcp ;;
    s1-agents|p2-agents|p4-agents|img-agents|img-coordinator|t1-agents|tr-agents) echo agents ;;
  esac
}

release() {   # release -> "name namespace"
  [[ "$(package_release "$1")" == "$1" ]] || die "package ${PACKAGE} changes the $(package_release "$1") release, not $1"
  case "$1" in
    mcp) echo "llm-agent-mcp ewallet-mcp" ;;
    agents) echo "llm-agent default" ;;
    *) die "release must be mcp or agents" ;;
  esac
}

CHECKS=" git_clean context_matches release_deployed save_live reproduces_live deployments_available drift_is_declarative redis_databases snapshot_manifest rag_registry transaction_input feature_readback apiserver_addresses drift_network token_secrets "

check() {     # check <label> <function> [args]: run in a fresh process with errexit; any FAIL fails preflight
  local label=$1; shift
  if "${BASH}" "${BASH_SOURCE[0]}" __check "$@" >>"${EV}/stage0/preflight.log" 2>&1; then
    log "PASS ${label}"
  else
    log "FAIL ${label}"
    return 1
  fi
}

# ---------------------------------------------------------------- Stage 0 (read-only)

git_clean() {
  [[ -z "$(git -C "${GIT_DIR_CHECKED}" status --porcelain --untracked-files=no)" ]] || return 1
  [[ "$(git -C "${GIT_DIR_CHECKED}" rev-parse HEAD)" == "${SHA}" ]]
}
context_matches() { [[ "$("${KUBECTL}" config current-context)" == "${CTX}" ]]; }
release_deployed() {
  local name=$1 namespace=$2 status revision
  status=$(hl -n "${namespace}" status "${name}" -o json)
  revision=$("${PYTHON}" -c 'import json,sys;d=json.load(sys.stdin);assert d["info"]["status"]=="deployed";print(d["version"])' <<< "${status}")
  echo "$(revision_key "${name}")=${revision}" >> "${EV}/stage0/revisions.env"
}
save_live() {
  local name=$1 namespace=$2
  hl -n "${namespace}" get values "${name}" -o yaml > "${PRIVATE}/${name}-values.yaml"
  hl -n "${namespace}" get manifest "${name}" > "${PRIVATE}/${name}-live.yaml"
}
reproduces_live() {
  local name=$1 namespace=$2
  hl template "${name}" "${CHART}" -n "${namespace}" -f "${PRIVATE}/${name}-values.yaml" > "${PRIVATE}/${name}-off.yaml"
  "${PYTHON}" "${ROOT}/scripts/llm_agent/helm_object_diff.py" "${PRIVATE}/${name}-live.yaml" "${PRIVATE}/${name}-off.yaml" \
    --expect-objects 0 --expect-identities > "${EV}/stage0/${name}-reproduce.txt"
}
deployments_available() {
  kc -n "$1" get deploy -o json | "${PYTHON}" -c '
import json, sys
items = json.load(sys.stdin)["items"]
bad = [d["metadata"]["name"] for d in items
       if d["spec"].get("replicas", 1) and d.get("status", {}).get("availableReplicas", 0) < d["spec"].get("replicas", 1)]
print("unavailable:", bad)
sys.exit(1 if bad or not items else 0)'
}
drift_is_declarative() {   # before P2 the drift-specialist is Declarative; P4 and image rolls run after P2 made it BYO
  local want=Declarative
  [[ "${PACKAGE}" == p4-agents || "${PACKAGE}" == img-* || "${PACKAGE}" == t1-agents || "${PACKAGE}" == tr-agents ]] && want=BYO
  [[ "$(kc -n ewallet-agents get agents.kagent.dev drift-specialist -o jsonpath='{.spec.type}')" == "${want}" ]]
}
redis_databases() {
  RAG_TARGET=gke RAG_KUBE_CONTEXT="${CTX}" "${RAG_RUNNER}" dbs > "${EV}/stage0/redis-dbs-before.txt"
  grep -Eq "^ +3 +5 +${RAG_PROJECT} " "${EV}/stage0/redis-dbs-before.txt"
}
snapshot_manifest() {
  local digest
  digest=$("${GCLOUD}" storage cat "${SNAPSHOT_URI}/_control/export_manifest.json" | sha256sum | cut -d' ' -f1)
  echo "${digest}" > "${EV}/stage0/snapshot-manifest-sha256.txt"
  [[ "${digest}" == "${SNAPSHOT_MANIFEST_SHA256}" ]]
}
rag_registry() {
  local uri=gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/${RAG_PROJECT}/registry.db digest
  "${GCLOUD}" storage ls -l "${uri}" > "${EV}/stage0/rag-registry.txt"
  digest=$("${GCLOUD}" storage cat "${uri}" | sha256sum | cut -d' ' -f1)
  echo "RAG_REGISTRY_SHA256=${digest}" >> "${EV}/stage0/revisions.env"
}
transaction_input() {
  "${PYTHON}" -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))["transaction"]))' "${REFERENCE_INPUT}" \
    > "${EV}/inputs/transaction.json"
}
token() { kc -n ewallet-agents get secret "$1" -o jsonpath='{.data.token}' | base64 -d; }
feature_readback() {   # read-only feature_lookup of the reference transaction (bounded DB 0 readback)
  local out=$1 arguments
  arguments=$("${PYTHON}" -c 'import json,sys;print(json.dumps({"transaction":json.load(open(sys.argv[1])),"feature_names":json.loads(sys.argv[2])}))' \
    "${EV}/inputs/transaction.json" "${FEATURES}")
  EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-feature-rag-specialist-feature-rag-mcp)" \
    "${AGENT_PY[@]}" "${ROOT}/scripts/llm_agent/agi_probe.py" mcp --endpoint "http://127.0.0.1:${AGI_MCP_PORT:-18082}/feature-rag/mcp" \
    --tool feature_lookup --arguments-json "${arguments}" --expect ok --evidence "${out}"
}
apiserver_addresses() {
  {
    kc get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}{"\n"}'
    kc get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[*].endpoints[*].addresses[*]}{"\n"}'
  } > "${EV}/stage0/apiserver.txt"
  [[ -s "${EV}/stage0/apiserver.txt" ]]
}
drift_network() { kc -n ewallet-agents get networkpolicy -o name | grep -q drift-specialist-to-mcp-gateway; }
token_secrets() {   # names only; values are never read here
  local name
  for name in mcp-token-feature-rag-specialist-feature-rag-mcp mcp-token-drift-specialist-drift-mcp \
              mcp-token-drift-specialist-feature-rag-mcp; do
    kc -n ewallet-agents get secret "${name}" -o name
  done
}

preflight() {
  rm -f "${EV}/stage0/PREFLIGHT_PASS" "${EV}/stage0/revisions.env"
  : > "${EV}/stage0/preflight.log"
  local failed=0
  check "P0.1 tracked tree clean at SHA" git_clean || failed=1
  check "P0.2 kube context" context_matches || failed=1
  check "P0.3 llm-agent deployed" release_deployed llm-agent default || failed=1
  check "P0.3 llm-agent-mcp deployed" release_deployed llm-agent-mcp ewallet-mcp || failed=1
  check "P0.4 live values saved (private)" save_live llm-agent default || failed=1
  check "P0.4 live values saved (private)" save_live llm-agent-mcp ewallet-mcp || failed=1
  check "P0.5 chart reproduces llm-agent" reproduces_live llm-agent default || failed=1
  check "P0.5 chart reproduces llm-agent-mcp" reproduces_live llm-agent-mcp ewallet-mcp || failed=1
  check "P0.6 agents available" deployments_available ewallet-agents || failed=1
  check "P0.6 MCP servers available" deployments_available ewallet-mcp || failed=1
  check "P0.7 drift-specialist is Declarative before the campaign" drift_is_declarative || failed=1
  check "P0.8 Redis DB 3 owned by the RAG Hybrid project" redis_databases || failed=1
  check "P0.9 drift snapshot manifest digest" snapshot_manifest || failed=1
  check "P0.10 RAG registry recorded" rag_registry || failed=1
  check "P0.11 reference transaction input" transaction_input || failed=1
  check "P0.12 Fraud target read (feature_lookup)" feature_readback "${EV}/stage0/fraud-target-precheck.json" || failed=1
  check "P0.13 API server addresses" apiserver_addresses || failed=1
  check "P0.14 drift-specialist egress to mcp-gateway" drift_network || failed=1
  check "P0.15 MCP credential Secrets exist (names only)" token_secrets || failed=1
  if (( failed )); then
    log "PREFLIGHT FAILED: see ${EV}/stage0/preflight.log; Stage 1 is blocked"
    return 1
  fi
  printf 'campaign=%s\nsha=%s\ncontext=%s\nsnapshot_manifest_sha256=%s\n' "${CAMPAIGN}" "${SHA}" "${CTX}" \
    "${SNAPSHOT_MANIFEST_SHA256}" > "${EV}/stage0/PREFLIGHT_PASS"
  cat "${EV}/stage0/revisions.env" >> "${EV}/stage0/PREFLIGHT_PASS"
  log "PREFLIGHT PASSED"
}

require_preflight() {
  local marker="${EV}/stage0/PREFLIGHT_PASS"
  [[ -f "${marker}" ]] || die "Stage 0 has not passed (${marker} missing)"
  grep -qx "sha=${SHA}" "${marker}" && grep -qx "campaign=${CAMPAIGN}" "${marker}" && grep -qx "context=${CTX}" "${marker}" \
    || die "Stage 0 passed for another campaign, commit or context"
  [[ "$(git -C "${GIT_DIR_CHECKED}" rev-parse HEAD)" == "${SHA}" && -z "$(git -C "${GIT_DIR_CHECKED}" status --porcelain --untracked-files=no)" ]] \
    || die "the checkout is no longer the clean commit ${SHA}"
  [[ -r "${PRIVATE}/llm-agent-values.yaml" && -r "${PRIVATE}/llm-agent-mcp-values.yaml" ]] \
    || die "live values from Stage 0 are missing in ${PRIVATE}"
}

# ---------------------------------------------------------------- Stage 1 gates

render_args() {   # render_args <mcp|agents> <array name>: complete argument list or failure
  local file="${PRIVATE}/$1-render-args.txt"
  "${BASH}" "${BASH_SOURCE[0]}" __args "$1" > "${file}"
  [[ "$(tail -n 1 "${file}")" == "__END__" ]] || die "render arguments for $1 are incomplete"
  mapfile -t "$2" < <(sed '$d' "${file}")
}

image_sha() {   # the commit the images were built from: SHA, or an ancestor with identical image inputs
  local image=${IMAGE_SHA:-${SHA}}
  [[ "${image}" =~ ^[0-9a-f]{40}$ ]] || die "IMAGE_SHA must be a full commit SHA"
  if [[ "${image}" != "${SHA}" ]]; then
    git -C "${GIT_DIR_CHECKED}" merge-base --is-ancestor "${image}" "${SHA}" || die "IMAGE_SHA ${image} is not an ancestor of ${SHA}"
    git -C "${GIT_DIR_CHECKED}" diff --quiet "${image}" "${SHA}" -- "${IMAGE_INPUTS[@]}" \
      || die "image inputs changed between IMAGE_SHA ${image} and ${SHA}; rebuild the images from ${SHA}"
  fi
  echo "${image}"
}

digest_ref() { [[ "$1" =~ ^[a-z0-9.-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+/[a-z0-9._-]+@sha256:[0-9a-f]{64}$ ]]; }

overlay_args() {   # prints one argument per line for this package's render of the release
  case "${PACKAGE}:$1" in
    agi:mcp|p1-mcp:mcp) agi_mcp_args
      [[ "${PACKAGE}" == p1-mcp ]] && printf '%s\n' -f "${CHART}/agi/mcp-reliability.yaml"
      echo __END__ ;;
    agi:agents|p2-agents:agents) agi_agent_args
      [[ "${PACKAGE}" == p2-agents ]] && printf '%s\n' -f "${CHART}/g3/keda.yaml" -f "${CHART}/agi/keda.yaml"
      echo __END__ ;;
    s1-mcp:mcp)
      need SANDBOX_SERVER_SET
      [[ "${SANDBOX_SERVER_SET}" =~ ^[a-z0-9]([-a-z0-9]{0,22}[a-z0-9])?$ ]] || die "SANDBOX_SERVER_SET must be a credential server-set name"
      printf '%s\n' -f "${PRIVATE}/llm-agent-mcp-values.yaml" -f "${CHART}/agi/mcp-sandbox-grants.yaml" \
        --set "mcpCredentialLifecycle.serverSet=${SANDBOX_SERVER_SET}"
      echo __END__ ;;
    img-mcp:mcp)
      need MCP_IMAGE
      local image_rev
      image_rev=$(image_sha) || exit 1
      digest_ref "${MCP_IMAGE}" || die "MCP_IMAGE must be a digest-pinned Artifact Registry reference"
      printf '%s\n' -f "${PRIVATE}/llm-agent-mcp-values.yaml" --set "mcpServices.image=${MCP_IMAGE}" \
        --set "mcpServices.identity.imageDigest=${MCP_IMAGE#*@}" --set "mcpServices.identity.gitSha=${image_rev}"
      echo __END__ ;;
    img-agents:agents)
      need COORDINATOR_IMAGE FEATURE_RAG_IMAGE DRIFT_IMAGE
      local image; for image in "${COORDINATOR_IMAGE}" "${FEATURE_RAG_IMAGE}" "${DRIFT_IMAGE}"; do
        digest_ref "${image}" || die "agent images must be digest-pinned Artifact Registry references"
      done
      printf '%s\n' -f "${PRIVATE}/llm-agent-values.yaml" \
        --set "agents.coordinator.byo.image=${COORDINATOR_IMAGE}" \
        --set "agents.specialists.feature-rag-specialist.byo.image=${FEATURE_RAG_IMAGE}" \
        --set "agents.specialists.drift-specialist.byo.image=${DRIFT_IMAGE}"
      echo __END__ ;;
    img-coordinator:agents)
      need COORDINATOR_IMAGE
      digest_ref "${COORDINATOR_IMAGE}" || die "COORDINATOR_IMAGE must be a digest-pinned Artifact Registry reference"
      printf '%s\n' -f "${PRIVATE}/llm-agent-values.yaml" --set "agents.coordinator.byo.image=${COORDINATOR_IMAGE}"
      echo __END__ ;;
    t1-agents:agents)
      (cd "${ROOT}" && "${PYTHON}" -m ops.llm_agent.agi_packages append-flow sandbox-otel-collector \
        --values "${PRIVATE}/llm-agent-values.yaml" --out "${PRIVATE}/t1-agents-flows.yaml" > /dev/null) \
        || die "cannot append the sandbox OTLP flow to the live crossNamespace.flows"
      printf '%s\n' -f "${PRIVATE}/llm-agent-values.yaml" -f "${PRIVATE}/t1-agents-flows.yaml"
      echo __END__ ;;
    tr-agents:agents)
      (cd "${ROOT}" && "${PYTHON}" -m ops.llm_agent.agi_packages append-flow kagent-controller-otel-collector \
        --values "${PRIVATE}/llm-agent-values.yaml" --out "${PRIVATE}/tr-agents-flows.yaml" > /dev/null) \
        || die "cannot append the kagent-controller OTLP flow to the live crossNamespace.flows"
      printf '%s\n' -f "${PRIVATE}/llm-agent-values.yaml" -f "${PRIVATE}/tr-agents-flows.yaml"
      echo __END__ ;;
    s1-agents:agents)
      (cd "${ROOT}" && "${PYTHON}" -m ops.llm_agent.agi_packages append-flow sandbox-mcp-gateway \
        --values "${PRIVATE}/llm-agent-values.yaml" --out "${PRIVATE}/s1-agents-flows.yaml" > /dev/null) \
        || die "cannot append the sandbox flow to the live crossNamespace.flows"
      printf '%s\n' -f "${PRIVATE}/llm-agent-values.yaml" -f "${PRIVATE}/s1-agents-flows.yaml"
      echo __END__ ;;
    p4-agents:agents)
      printf '%s\n' -f "${PRIVATE}/llm-agent-values.yaml" --set a2aGateway.sandboxRoutes.namespace=ewallet-sandbox \
        --set "a2aGateway.sandboxRoutes.agents=${SANDBOX_SPECIALISTS}" \
        --set "agents.coordinator.byo.sandboxSpecialists=${SANDBOX_SPECIALISTS}"
      echo __END__ ;;
    *) die "package ${PACKAGE} does not change release $1" ;;
  esac
}

agi_mcp_args() {
  need MCP_IMAGE DRIFT_DESCRIBE FRAUD_DB RAG_DB
  local image_rev
  image_rev=$(image_sha) || exit 1   # a failure inside $(...) must not let __END__ through
  digest_ref "${MCP_IMAGE}" || die "MCP_IMAGE must be a digest-pinned Artifact Registry reference"
  [[ "${FRAUD_DB}" =~ ^([0-9]|1[0-5])$ && "${RAG_DB}" =~ ^([2-9]|1[0-5])$ ]] || die "FRAUD_DB 0-15 and RAG_DB 2-15 required"
  local pins
  pins=$("${PYTHON}" -c '
import json, sys
env = json.load(open(sys.argv[1]))["environment"]
assert env["EWALLET_DRIFT_ALLOW_SYNTHETIC"] == "false", "synthetic drift artifacts"
for key in ("EWALLET_DRIFT_BASELINES_SHA256", "EWALLET_DRIFT_WINDOW_MANIFEST_SHA256", "EWALLET_DRIFT_DATASET_REVISION"):
    print(f"mcpServices.services.drift-mcp.env.{key}={env[key]}")' "${DRIFT_DESCRIBE}")
  printf '%s\n' -f "${PRIVATE}/llm-agent-mcp-values.yaml" -f "${CHART}/agi/mcp.yaml" \
    --set "mcpServices.image=${MCP_IMAGE}" --set "mcpServices.identity.imageDigest=${MCP_IMAGE#*@}" \
    --set "mcpServices.identity.gitSha=${image_rev}" --set "mcpServices.identity.configRevision=${CAMPAIGN}" \
    --set-string "mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ONLINE_DB=${FRAUD_DB}" \
    --set-string "mcpServices.services.feature-rag-mcp.env.EWALLET_RAG_ONLINE_DB=${RAG_DB}"
  printf '%s\n' --set-string "mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ALLOW_PRODUCTION_DB=$([[ "${FRAUD_DB}" == 0 ]] && echo true || echo false)"
  local pin; while read -r pin; do printf '%s\n' --set "${pin}"; done <<< "${pins}"
}

agi_agent_args() {
  need COORDINATOR_IMAGE FEATURE_RAG_IMAGE DRIFT_IMAGE APISERVER_CIDRS
  local image; for image in "${COORDINATOR_IMAGE}" "${FEATURE_RAG_IMAGE}" "${DRIFT_IMAGE}"; do
    digest_ref "${image}" || die "agent images must be digest-pinned Artifact Registry references"
  done
  [[ "${APISERVER_CIDRS}" =~ ^[0-9./]+(,[0-9./]+)*$ ]] || die "APISERVER_CIDRS must be a comma list of /32 CIDRs"
  printf '%s\n' -f "${PRIVATE}/llm-agent-values.yaml" -f "${CHART}/agi/agents.yaml" \
    --set "agents.coordinator.byo.image=${COORDINATOR_IMAGE}" \
    --set "agents.specialists.feature-rag-specialist.byo.image=${FEATURE_RAG_IMAGE}" \
    --set "agents.specialists.drift-specialist.byo.image=${DRIFT_IMAGE}" \
    --set "modelConfig.byoResolver.apiServer.cidrs={${APISERVER_CIDRS}}"
}

expected_identities() {
  local agi_agents=(--expect-changed Agent/ewallet-agents/fraud-coordinator
    --expect-changed Agent/ewallet-agents/feature-rag-specialist --expect-changed Agent/ewallet-agents/drift-specialist
    --expect-added Role/ewallet-agents/ewallet-global-llm-reader
    --expect-added RoleBinding/ewallet-agents/ewallet-global-llm-reader
    --expect-added NetworkPolicy/ewallet-agents/allow-byo-modelconfig-to-kube-apiserver)
  local agent
  case "${PACKAGE}:$1" in
    agi:mcp|s1-mcp:mcp|img-mcp:mcp) printf '%s\n' --expect-changed Deployment/ewallet-mcp/feature-rag-mcp --expect-changed Deployment/ewallet-mcp/drift-mcp ;;
    p1-mcp:mcp) printf '%s\n' --expect-changed Deployment/ewallet-mcp/feature-rag-mcp --expect-changed Deployment/ewallet-mcp/drift-mcp \
                  --expect-added PodDisruptionBudget/ewallet-mcp/feature-rag-mcp ;;
    agi:agents) printf '%s\n' "${agi_agents[@]}" ;;
    img-agents:agents) printf '%s\n' --expect-changed Agent/ewallet-agents/fraud-coordinator \
                  --expect-changed Agent/ewallet-agents/feature-rag-specialist --expect-changed Agent/ewallet-agents/drift-specialist ;;
    p2-agents:agents) printf '%s\n' "${agi_agents[@]}" \
                  --expect-changed ConfigMap/ewallet-agents/otel-collector-config --expect-changed Deployment/ewallet-agents/otel-collector \
                  --expect-annotation-only Deployment/ewallet-agents/prometheus=checksum/observability-config \
                  --expect-annotation-only Deployment/ewallet-agents/grafana=checksum/observability-config
      for agent in ${KEDA_AGENTS}; do
        printf '%s\n' --expect-added "ScaledObject/ewallet-agents/${agent}" --expect-removed "HorizontalPodAutoscaler/ewallet-agents/${agent}"
      done ;;
    img-coordinator:agents) printf '%s\n' --expect-changed Agent/ewallet-agents/fraud-coordinator ;;
    t1-agents:agents) printf '%s\n' --expect-added AuthorizationPolicy/ewallet-agents/allow-sandbox-workers-to-otel-collector \
                  --expect-added NetworkPolicy/ewallet-agents/allow-sandbox-workers-to-otel-collector-ingress ;;
    tr-agents:agents) printf '%s\n' --expect-added AuthorizationPolicy/ewallet-agents/allow-kagent-controller-to-otel-collector \
                  --expect-added NetworkPolicy/ewallet-agents/allow-kagent-controller-to-otel-collector-ingress ;;
    s1-agents:agents) printf '%s\n' --expect-added AuthorizationPolicy/ewallet-agents/allow-sandbox-workers-to-mcp-gateway \
                  --expect-added NetworkPolicy/ewallet-agents/allow-sandbox-workers-to-mcp-gateway-ingress ;;
    p4-agents:agents) printf '%s\n' --expect-changed Agent/ewallet-agents/fraud-coordinator \
                  --expect-changed ConfigMap/ewallet-agents/a2a-gateway-config --expect-changed Deployment/ewallet-agents/a2a-gateway ;;
  esac
}

gate() {   # read-only: fails before any mutating command can run
  local which=$1 name namespace
  read -r name namespace <<< "$(release "${which}")"
  require_preflight
  local -a args identities
  render_args "${which}" args
  mapfile -t identities < <(expected_identities "${which}")
  hl -n "${namespace}" get manifest "${name}" > "${PRIVATE}/${name}-live-now.yaml"
  cmp -s "${PRIVATE}/${name}-live-now.yaml" "${PRIVATE}/${name}-live.yaml" \
    || die "${name} changed since Stage 0; re-run preflight"
  hl template "${name}" "${CHART}" -n "${namespace}" "${args[@]}" > "${PRIVATE}/${name}-on.yaml"
  "${PYTHON}" "${ROOT}/scripts/llm_agent/helm_object_diff.py" "${PRIVATE}/${name}-live.yaml" "${PRIVATE}/${name}-on.yaml" \
    --expect-identities "${identities[@]}" > "${EV}/stage1/${name}-diff.txt"
  hl upgrade "${name}" "${CHART}" -n "${namespace}" "${args[@]}" --dry-run=server > "${PRIVATE}/${name}-dry-run.txt"
  sha256sum "${PRIVATE}/${name}-on.yaml" | cut -d' ' -f1 > "${EV}/stage1/${name}-render-sha256.txt"
  # <release>-diff.txt is the latest gate; the per-package copies keep every package's reviewed diff.
  cp "${EV}/stage1/${name}-diff.txt" "${EV}/stage1/${name}-${PACKAGE}-diff.txt"
  cp "${EV}/stage1/${name}-render-sha256.txt" "${EV}/stage1/${name}-${PACKAGE}-render-sha256.txt"
  log "GATE PASS ${name}: exact identities and server-side dry-run"
}

require_approval() {   # require_approval <A1|A2>: the agi package keeps A1/A2; others name themselves
  local token=$1
  if [[ "${PACKAGE}" != agi ]]; then
    token=$(echo "${PACKAGE}" | tr 'a-z' 'A-Z')
    [[ "$1" == A2 ]] && token="${token}-ROLLBACK"
  fi
  [[ "${AGI_APPROVAL:-}" == "${token}:${CAMPAIGN}" ]] || die "approval ${token} for ${CAMPAIGN} is required (AGI_APPROVAL=${token}:${CAMPAIGN})"
}

upgrade() {
  local which=$1 name namespace
  read -r name namespace <<< "$(release "${which}")"
  require_approval A1
  if [[ "${PACKAGE}" == agi || "${PACKAGE}" == p2-agents ]]; then
    [[ "${which}" == mcp || -f "${EV}/stage1/llm-agent-mcp.UPGRADED" ]] || die "upgrade the MCP release first"
  fi
  gate "${which}"
  local -a args; render_args "${which}" args
  log "MUTATION helm upgrade ${name} (${namespace})"
  hl upgrade "${name}" "${CHART}" -n "${namespace}" "${args[@]}" --atomic --timeout 10m > "${EV}/stage1/${name}-${PACKAGE}-upgrade.txt"
  hl -n "${namespace}" history "${name}" --max 2 > "${EV}/stage1/${name}-${PACKAGE}-history-after.txt"
  date -u +%FT%TZ > "${EV}/stage1/${name}.UPGRADED"
  date -u +%FT%TZ > "${EV}/stage1/${name}.${PACKAGE}.UPGRADED"
  log "UPGRADED ${name}"
}

# ---------------------------------------------------------------- Stage 2 negative probes

with_tokens() {   # with_tokens <tested> <control> <command...>: tokens reach the probe via its environment, never argv
  ( export EWALLET_MCP_SERVICE_TOKEN="$1" AGI_CONTROL_TOKEN="$2"; shift 2; "$@" )
}

expect_probe() {   # the probe exits 0 only when it observed exactly the expected outcome
  local label=$1; shift
  if "$@"; then log "PASS ${label}"; else log "FAIL ${label}"; return 1; fi
}

negative() {
  # Two MCP mechanisms and one A2A mechanism, each proven by a control:
  #  1. per-server credentials: the Feature-RAG credential is refused (HTTP 401) by drift-mcp, while it is
  #     valid on feature-rag-mcp and the drift credential lists drift_detection;
  #  2. per-tool grants: drift-specialist's feature-rag-mcp credential does not see fraud_scoring (hidden
  #     "Unknown tool"), while the Feature-RAG credential lists it;
  #  3. A2A routes: an undeclared x-kagent-host gets 404 while drift-specialist is routed.
  need BASELINE
  local window='{"start":"2026-05-01T00:00:00Z","end":"2026-05-31T00:00:00Z"}' failed=0 mcp="http://127.0.0.1:${AGI_MCP_PORT:-18082}"
  local drift_args fraud_args
  drift_args="{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"${BASELINE}\",\"current_window\":${window}}"
  fraud_args="{\"transaction\":$(cat "${EV}/inputs/transaction.json")}"
  local rag drift drift_on_rag
  rag=$(token mcp-token-feature-rag-specialist-feature-rag-mcp)
  drift=$(token mcp-token-drift-specialist-drift-mcp)
  drift_on_rag=$(token mcp-token-drift-specialist-feature-rag-mcp)
  expect_probe "AGI-08 feature-rag credential refused by drift-mcp" with_tokens "${rag}" "${drift}" "${AGENT_PY[@]}" "${ROOT}/scripts/llm_agent/agi_probe.py" mcp \
    --endpoint "${mcp}/drift/mcp" --tool drift_detection --arguments-json "${drift_args}" --expect unauthenticated \
    --control-token-env AGI_CONTROL_TOKEN --tested-valid-endpoint "${mcp}/feature-rag/mcp" \
    --evidence "${EV}/stage2/AGI-08-mcp-wrong-server-credential.json" || failed=1
  expect_probe "AGI-08 drift principal does not see fraud_scoring" with_tokens "${drift_on_rag}" "${rag}" "${AGENT_PY[@]}" "${ROOT}/scripts/llm_agent/agi_probe.py" mcp \
    --endpoint "${mcp}/feature-rag/mcp" --tool fraud_scoring --arguments-json "${fraud_args}" --expect denied \
    --control-token-env AGI_CONTROL_TOKEN --evidence "${EV}/stage2/AGI-08-mcp-ungranted-tool.json" || failed=1
  "${AGENT_PY[@]}" "${ROOT}/scripts/llm_agent/agi_probe.py" request drift --request-id "${CAMPAIGN}-a2a-neg" \
    --feature account_behavior__account_tx_count_1h --baseline "${BASELINE}" \
    --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z --out "${EV}/stage2/AGI-08-a2a-request.json"
  expect_probe "AGI-08 undeclared A2A host is not routed" "${AGENT_PY[@]}" "${ROOT}/scripts/llm_agent/agi_probe.py" a2a-route \
    --endpoint "http://127.0.0.1:${AGI_A2A_PORT:-18083}/" --kagent-host payments-agent.ewallet-agents \
    --control-kagent-host drift-specialist.ewallet-agents --request "${EV}/stage2/AGI-08-a2a-request.json" \
    --expect no-route --evidence "${EV}/stage2/AGI-08-a2a-unrouted.json" || failed=1
  (( ! failed )) || die "AGI-08: at least one negative probe did not prove its expected denial"
  log "AGI-08 PASS"
}

# ---------------------------------------------------------------- data safety (read-only)

data_state() {
  local phase=$1 out="${EV}/stage$([[ $1 == before ]] && echo 0 || echo 5)"
  [[ "${phase}" == before || "${phase}" == after ]] || die "data-state before|after"
  RAG_TARGET=gke RAG_KUBE_CONTEXT="${CTX}" "${RAG_RUNNER}" dbs > "${out}/redis-dbs-${phase}.txt"
  RAG_TARGET=gke RAG_KUBE_CONTEXT="${CTX}" "${RAG_RUNNER}" digest 3 > "${out}/redis-db3-digest-${phase}.txt"
  feature_readback "${out}/db0-readback-${phase}.json"
  if [[ "${phase}" == after ]]; then
    diff "${EV}/stage0/redis-dbs-before.txt" "${EV}/stage5/redis-dbs-after.txt" || die "Redis key counts or owners changed"
    diff "${EV}/stage0/redis-db3-digest-before.txt" "${EV}/stage5/redis-db3-digest-after.txt" || die "DB 3 content changed"
    "${PYTHON}" - "${EV}/stage0/db0-readback-before.json" "${EV}/stage5/db0-readback-after.json" <<'EOF' || die "DB 0 readback changed"
import json, sys
before, after = (json.load(open(path))["structured_content"]["result"]["historical_features"] for path in sys.argv[1:])
sys.exit(0 if before == after else 1)
EOF
    log "DATA STATE UNCHANGED (key counts and owners of DB 0-15, DB 3 content digest, DB 0 reference readback)"
  fi
}

# ---------------------------------------------------------------- Stage 5 rollback

rollback() {
  local which=$1 name namespace revision key
  read -r name namespace <<< "$(release "${which}")"
  require_approval A2
  key=$(revision_key "${name}")
  revision=$(grep -E "^${key}=" "${EV}/stage0/PREFLIGHT_PASS" | cut -d= -f2)
  [[ "${revision}" =~ ^[0-9]+$ ]] || die "no Stage 0 revision recorded for ${name}"
  if [[ "${PACKAGE}" == p2-agents ]]; then
    # A plain rollback deletes the ScaledObjects, which cascades to the HPAs KEDA adopted and leaves
    # no autoscaler. Delete the three ScaledObjects first; KEDA then releases the HPAs and the
    # rollback restores the chart's own HPAs.
    local agent
    for agent in ${KEDA_AGENTS}; do
      log "MUTATION delete ScaledObject ewallet-agents/${agent} before the rollback"
      kc -n ewallet-agents delete scaledobject "${agent}" --wait=true --timeout=120s > /dev/null
    done
  fi
  log "MUTATION helm rollback ${name} to ${revision}"
  hl -n "${namespace}" rollback "${name}" "${revision}" --wait --timeout 10m > "${EV}/stage5/${name}-rollback.txt"
  hl -n "${namespace}" get manifest "${name}" > "${PRIVATE}/${name}-after-rollback.yaml"
  "${PYTHON}" "${ROOT}/scripts/llm_agent/helm_object_diff.py" "${PRIVATE}/${name}-live.yaml" "${PRIVATE}/${name}-after-rollback.yaml" \
    --expect-objects 0 --expect-identities > "${EV}/stage5/${name}-rollback-diff.txt"
  if [[ "${PACKAGE}" == p2-agents ]]; then
    for agent in ${KEDA_AGENTS}; do
      kc -n ewallet-agents get hpa "${agent}" -o jsonpath='{.spec.minReplicas}' > /dev/null \
        || die "HPA ewallet-agents/${agent} is missing after the rollback"
    done
  fi
  log "ROLLED BACK ${name}: manifest equals the Stage 0 manifest"
}

main() {
  local command=${1:-}
  case "${command}" in
    preflight) load; preflight ;;
    gate) load; gate "${2:?mcp|agents}" ;;
    upgrade) load; upgrade "${2:?mcp|agents}" ;;
    negative) load; require_preflight; negative ;;
    data-state) load; data_state "${2:?before|after}" ;;
    rollback) load; rollback "${2:?mcp|agents}" ;;
    __check) load; [[ "${CHECKS}" == *" ${2:-} "* ]] || die "unknown check ${2:-}"; "${@:2}" ;;
    __args) load; overlay_args "${2:?mcp|agents}" ;;
    __identities) load; expected_identities "${2:?mcp|agents}" ;;   # read-only: the gate's expected changes
    *) sed -n '2,15p' "$0"; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
