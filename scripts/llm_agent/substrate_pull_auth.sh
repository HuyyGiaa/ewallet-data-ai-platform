#!/usr/bin/env bash
# PKG-R: let Substrate's atelet authenticate its own Artifact Registry pulls.
# Release substrate (namespace ate-system), pinned chart 0.0.9, pilot post-renderer, overlay
# infra/kubernetes/kagent-pilot/values/substrate-ar-pull-overlay.yaml (atelet.gcpAuthForImagePulls).
# The IAM read grant is a separate package: scripts/llm_agent/substrate_pull_iam.sh (PKG-R-IAM).
#
# usage: substrate_pull_auth.sh <action> <private-work-dir>
#   preflight  [READ-ONLY]  pinned chart digest, release deployed, atelet identity (ServiceAccount
#                           ate-system/atelet, no hostNetwork, no GSA annotation, flag still false),
#                           Workload Identity on cluster and pool, live values/manifest saved (mode
#                           600), identity: chart + live values + post-renderer, rendered with the
#                           chart's lookup against the cluster, == the live manifest
#   diff       [READ-ONLY]  preflight, then the exact overlay diff (only atelet args[0])
#   dry-run    [READ-ONLY]  diff, then helm upgrade --dry-run=server
#   upgrade    [MUTATES]    PKG_R_APPROVAL=PKG-R:<context>; needs the PKG-R-IAM binding and a Ready
#                           gke-metadata-server on every atelet node; diff and a server-side dry-run
#                           in the same execution, then helm upgrade --atomic; verify
#   verify     [READ-ONLY]  flag true, atelet rolled out, no restart, no authenticator failure
#   rollback   [MUTATES]    PKG_R_APPROVAL=PKG-R-ROLLBACK:<context>; helm rollback to the recorded
#                           revision, flag false, manifest identical to the pre-upgrade release
#   cleanup    [LOCAL]      delete the private renders (they hold the live TLS key and signing pools)
#
# The renders read Secrets through lookup, so the work directory is private (mode 700) and no
# output of this script prints a value: diffs name objects and field paths only.
# Env: KUBE_CONTEXT (required), KUBECONFIG, HELM, KUBECTL, GCLOUD.
set -euo pipefail
action=${1:-} work=${2:-}
die() { echo "substrate_pull_auth: FAIL: $*" >&2; exit 1; }
case "$action" in preflight|diff|dry-run|upgrade|verify|rollback|cleanup) ;; *) die "usage: substrate_pull_auth.sh <action> <work-dir>";; esac
[[ -n "$work" ]] || die "work directory required"
[[ -n "${KUBE_CONTEXT:-}" ]] || die "KUBE_CONTEXT is required"
root=$(cd "$(dirname "$0")/../.." && pwd)
helm=${HELM:-helm} kubectl=${KUBECTL:-kubectl} gcloud=${GCLOUD:-gcloud}
umask 077
mkdir -p "$work/substrate"
chmod 700 "$work" "$work/substrate"
work=$(cd "$work" && pwd)/substrate
release=substrate namespace=ate-system
PROJECT=ewallet-data-ai-platform-hg26 CLUSTER=ewallet-dev LOCATION=asia-southeast1-b POOL=cpu-pool
overlay=$root/infra/kubernetes/kagent-pilot/values/substrate-ar-pull-overlay.yaml
renderer=(--post-renderer "$root/infra/kubernetes/kagent-pilot/scripts/substrate_resource_post_renderer.py")
# shellcheck source=../../infra/kubernetes/kagent-pilot/versions.env
. "$root/infra/kubernetes/kagent-pilot/versions.env"
H() { "$helm" --kube-context "$KUBE_CONTEXT" "$@"; }
K() { "$kubectl" --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$work/package.log"; }
diff_tool() { python3 "$root/scripts/llm_agent/substrate_pull_diff.py" "$@"; }
flag() { K -n "$namespace" get daemonset atelet -o 'jsonpath={.spec.template.spec.containers[0].args[0]}'; }

if [[ "$action" == cleanup ]]; then
  rm -f "$work"/render-*.yaml "$work"/manifest-*.yaml "$work"/values-live.yaml "$work"/dry-run.yaml
  log "cleanup: private renders, manifests and values removed"
  exit 0
fi

fetch_chart() {
  rm -rf "$work/chart" && mkdir -p "$work/chart"
  "$helm" pull "$SUBSTRATE_CHART" --version "$SUBSTRATE_VERSION" --destination "$work/chart" > "$work/chart/pull.txt" 2>&1 \
    || die "helm pull substrate failed"
  grep -q "Digest: $SUBSTRATE_CHART_DIGEST" "$work/chart/pull.txt" || die "substrate chart digest is not $SUBSTRATE_CHART_DIGEST"
  chart=$work/chart/substrate-$SUBSTRATE_VERSION.tgz
}

identity_checks() {   # the principal PKG-R-IAM grants must be the one atelet's ADC resolves to
  local sa host annotation pool_mode workload_pool
  sa=$(K -n "$namespace" get daemonset atelet -o 'jsonpath={.spec.template.spec.serviceAccountName}')
  [[ "$sa" == atelet ]] || die "atelet runs as ServiceAccount '$sa', not atelet; the IAM principal would differ"
  host=$(K -n "$namespace" get daemonset atelet -o 'jsonpath={.spec.template.spec.hostNetwork}')
  [[ "$host" != true ]] || die "atelet uses hostNetwork: ADC would bypass the GKE metadata server and use the node identity"
  annotation=$(K -n "$namespace" get serviceaccount atelet -o 'jsonpath={.metadata.annotations.iam\.gke\.io/gcp-service-account}')
  [[ -z "$annotation" ]] || die "ServiceAccount atelet is bound to $annotation; the reviewed principal is the federated KSA principal"
  workload_pool=$("$gcloud" container clusters describe "$CLUSTER" --location "$LOCATION" --project "$PROJECT" \
    --format='value(workloadIdentityConfig.workloadPool)')
  [[ "$workload_pool" == "$PROJECT.svc.id.goog" ]] || die "Workload Identity pool is '$workload_pool'"
  pool_mode=$("$gcloud" container node-pools describe "$POOL" --cluster "$CLUSTER" --location "$LOCATION" --project "$PROJECT" \
    --format='value(config.workloadMetadataConfig.mode)')
  [[ "$pool_mode" == GKE_METADATA ]] || die "node pool $POOL metadata mode is '$pool_mode', not GKE_METADATA"
  log "identity: ServiceAccount ate-system/atelet, no hostNetwork, no GSA binding, pool $workload_pool, $POOL GKE_METADATA"
}

runtime_checks() {   # before a mutation: the grant exists and every atelet node serves Workload Identity
  "$root/scripts/llm_agent/substrate_pull_iam.sh" verify > "$work/iam-verify.txt" 2>&1 \
    || { cat "$work/iam-verify.txt" >&2; die "the PKG-R-IAM binding is missing; grant it first"; }
  local node missing=""
  for node in $(K -n "$namespace" get pods -l app=atelet -o 'jsonpath={.items[*].spec.nodeName}'); do
    [[ "$(K get node "$node" -o 'jsonpath={.metadata.labels.iam\.gke\.io/gke-metadata-server-enabled}')" == true ]] || missing+=" $node(label)"
    K -n kube-system get pods -l k8s-app=gke-metadata-server --field-selector "spec.nodeName=$node,status.phase=Running" \
      -o name | grep -q . || missing+=" $node(pod)"
  done
  [[ -z "$missing" ]] || die "gke-metadata-server is not serving on:$missing"
  log "runtime: PKG-R-IAM binding present; gke-metadata-server Running on every atelet node"
}

preflight() {
  fetch_chart
  local status
  status=$(H status "$release" -n "$namespace" -o json) || die "release $namespace/$release not found"
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["info"]["status"]=="deployed", d["info"]["status"]; print(d["version"])' \
    <<< "$status" > "$work/revision-live" || die "release $release is not in status deployed"
  identity_checks
  [[ "$(flag)" == "--gcp-auth-for-image-pulls=false" ]] || die "atelet args[0] is not --gcp-auth-for-image-pulls=false; nothing to do or unexpected shape"
  H get values "$release" -n "$namespace" -o yaml > "$work/values-live.yaml"
  H get manifest "$release" -n "$namespace" > "$work/manifest-live.yaml"
  # --dry-run=server enables the chart's lookup, so generated CA/TLS/session pools are read back,
  # not regenerated: the baseline must then equal the live manifest object for object.
  H template "$release" "$chart" -n "$namespace" -f "$work/values-live.yaml" "${renderer[@]}" --dry-run=server \
    > "$work/render-baseline.yaml" 2> "$work/render-baseline.err" || die "baseline render failed"
  diff_tool identity "$work/manifest-live.yaml" "$work/render-baseline.yaml" > "$work/identity.txt" 2>&1 \
    || { cat "$work/identity.txt" >&2; die "chart + live values + post-renderer do not reproduce the live release; stop"; }
  log "preflight PASS: $namespace/$release revision $(cat "$work/revision-live"), identity PASS (lookup render)"
}

candidate() {
  H template "$release" "$chart" -n "$namespace" -f "$work/values-live.yaml" -f "$overlay" "${renderer[@]}" --dry-run=server \
    > "$work/render-candidate.yaml" 2> "$work/render-candidate.err" || die "candidate render failed"
  diff_tool candidate "$work/render-baseline.yaml" "$work/render-candidate.yaml" > "$work/diff.txt" 2>&1 \
    || { cat "$work/diff.txt" >&2; die "overlay diff is not the reviewed diff (only atelet args[0])"; }
  log "diff PASS: $(tr -d '\n ' < "$work/diff.txt" | cut -c1-300)"
}

upgrade_args() { echo upgrade "$release" "$chart" -n "$namespace" --reuse-values -f "$overlay"; }

server_dry_run() {
  # shellcheck disable=SC2046
  H $(upgrade_args) "${renderer[@]}" --dry-run=server > "$work/dry-run.yaml" 2> "$work/dry-run.err" \
    || { tail -3 "$work/dry-run.err" >&2; die "server-side dry-run failed; nothing was upgraded"; }
  log "dry-run PASS (server-side)"
}

verify() {
  local want=$1 pods restarts
  [[ "$(flag)" == "$want" ]] || die "atelet args[0] is '$(flag)', expected '$want'"
  K -n "$namespace" rollout status daemonset/atelet --timeout=600s > /dev/null || die "atelet is not rolled out"
  restarts=$(K -n "$namespace" get pods -l app=atelet -o 'jsonpath={range .items[*]}{.metadata.name}={.status.containerStatuses[0].restartCount} {end}')
  [[ -n "$restarts" ]] || die "no atelet pods"
  if [[ "$restarts" =~ =[1-9] ]]; then die "atelet restarted after the change: $restarts"; fi
  for pods in $(K -n "$namespace" get pods -l app=atelet -o 'jsonpath={.items[*].metadata.name}'); do
    if K -n "$namespace" logs "$pods" --tail=200 | grep -q "Failed to create GCP registry authenticator"; then
      die "atelet $pods could not obtain a Google token (Workload Identity); roll back"
    fi
  done
  log "verify PASS: args[0] $want; atelet rolled out, restarts: $restarts"
}

approval() { [[ "${PKG_R_APPROVAL:-}" == "$1" ]] || die "set PKG_R_APPROVAL=$1 only after the owner approves this exact package"; }

case "$action" in
  preflight) preflight ;;
  diff) preflight; candidate ;;
  dry-run) preflight; candidate; server_dry_run ;;
  upgrade)
    approval "PKG-R:$KUBE_CONTEXT"
    preflight; runtime_checks; candidate; server_dry_run
    cp "$work/revision-live" "$work/revision-before-upgrade"
    log "upgrade: $(upgrade_args) (from revision $(cat "$work/revision-before-upgrade"))"
    # shellcheck disable=SC2046
    H $(upgrade_args) "${renderer[@]}" --atomic --wait --timeout 15m > "$work/upgrade.txt" 2>&1 \
      || { tail -3 "$work/upgrade.txt" >&2; die "helm upgrade failed (--atomic rolled it back)"; }
    verify "--gcp-auth-for-image-pulls=true" ;;
  verify) verify "--gcp-auth-for-image-pulls=true" ;;
  rollback)
    approval "PKG-R-ROLLBACK:$KUBE_CONTEXT"
    [[ -s "$work/revision-before-upgrade" && -s "$work/render-baseline.yaml" ]] || die "no recorded pre-upgrade revision or baseline in $work"
    before=$(cat "$work/revision-before-upgrade")
    H rollback "$release" "$before" -n "$namespace" --wait --timeout 15m > "$work/rollback.txt" 2>&1 \
      || { tail -3 "$work/rollback.txt" >&2; die "helm rollback failed"; }
    verify "--gcp-auth-for-image-pulls=false"
    H get manifest "$release" -n "$namespace" > "$work/manifest-after-rollback.yaml"
    diff_tool identity "$work/manifest-after-rollback.yaml" "$work/render-baseline.yaml" > "$work/identity-after-rollback.txt" 2>&1 \
      || { cat "$work/identity-after-rollback.txt" >&2; die "rolled-back release differs from the pre-upgrade manifest"; }
    log "rollback PASS: revision $before restored, manifest identical to the pre-upgrade release" ;;
esac
