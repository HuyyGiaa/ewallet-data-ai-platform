#!/usr/bin/env bash
# PKG-T: runtime-stability images (resume-rca/RCA.md section 8) on the existing releases.
#   kagent     release kagent/kagent: controller image -> patched 0.10.3 build, plus
#              SUBSTRATE_CALL_TIMEOUT (values overlay kagent-runtime-fix-overlay.yaml)
#   substrate  release ate-system/substrate: ate-api-server image -> patched 0.0.9 build
#              (substrate post-renderer --ateapi-image; the chart has one tag for all components),
#              and atelet memory (--atelet-memory; the in-memory pull cache OOM-killed it at 512Mi).
#              A later patched build replaces an earlier one: the baseline renders the live patched image.
#   kagent-otel release kagent/kagent (P2-C): the controller's OTLP traces endpoint gets its http:// scheme
#              (values overlay kagent-otel-endpoint-overlay.yaml); image and env unchanged. Apply only
#              after the agents package tr-agents admitted kagent-controller -> otel-collector:4317.
# Each target is its own approval. Images come from build_runtime_fix_images.py + scan_and_push.sh;
# their digests are pinned in infra/kubernetes/kagent-pilot/patches/runtime-fix-images.env.
#
# usage: runtime_fix.sh <kagent|substrate|kagent-otel> <action> <private-work-dir>
#   preflight  [READ-ONLY]  pinned chart digest, release deployed, controller Ready, live values and
#                           manifest saved (mode 600), identity: chart + live values + post-renderer
#                           == live manifest; records the live revision and image digests
#   diff       [READ-ONLY]  preflight, then the exact diff (runtime_fix_diff.py)
#   dry-run    [READ-ONLY]  diff, then helm upgrade --dry-run=server
#   upgrade    [MUTATES]    RUNTIME_FIX_APPROVAL=<package>:<context> (PKG-T-KAGENT, PKG-T-SUBSTRATE,
#                           P2C-KAGENT-OTEL); diff and dry-run in
#                           the same execution, then helm upgrade --atomic; verify
#   verify     [READ-ONLY]  running pod's imageID is the patched digest, rolled out, no restarts
#   rollback   [MUTATES]    RUNTIME_FIX_APPROVAL=PKG-T-<...>-ROLLBACK:<context>; helm rollback to the
#                           recorded revision; manifest identical to the pre-upgrade release
#   cleanup    [LOCAL]      delete private renders (the substrate render holds live TLS material)
#
# Env: KUBE_CONTEXT (required), KUBECONFIG, HELM, KUBECTL.
set -euo pipefail
target=${1:-} action=${2:-} work=${3:-}
die() { echo "runtime_fix: FAIL: $*" >&2; exit 1; }
case "$target" in kagent|substrate|kagent-otel) ;; *) die "usage: runtime_fix.sh <kagent|substrate|kagent-otel> <action> <work-dir>";; esac
case "$action" in preflight|diff|dry-run|upgrade|verify|rollback|cleanup) ;; *) die "unknown action '$action'";; esac
[[ -n "$work" ]] || die "work directory required"
[[ -n "${KUBE_CONTEXT:-}" ]] || die "KUBE_CONTEXT is required"
root=$(cd "$(dirname "$0")/../.." && pwd)
helm=${HELM:-helm} kubectl=${KUBECTL:-kubectl}
umask 077
mkdir -p "$work/runtime-fix-$target"
chmod 700 "$work" "$work/runtime-fix-$target"
work=$(cd "$work" && pwd)/runtime-fix-$target
H() { "$helm" --kube-context "$KUBE_CONTEXT" "$@"; }
K() { "$kubectl" --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
log() { printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$target" "$*" | tee -a "$work/package.log"; }
diff_tool() { python3 "$root/scripts/llm_agent/runtime_fix_diff.py" "$@"; }
# shellcheck source=../../infra/kubernetes/kagent-pilot/versions.env
. "$root/infra/kubernetes/kagent-pilot/versions.env"
# shellcheck source=../../infra/kubernetes/kagent-pilot/patches/runtime-fix-images.env
. "$root/infra/kubernetes/kagent-pilot/patches/runtime-fix-images.env"

if [[ $target == kagent ]]; then
  package=PKG-T-KAGENT release=kagent namespace=kagent deployment=kagent-controller container=controller
  chart_ref=$KAGENT_CHART chart_version=$KAGENT_VERSION chart_digest=$KAGENT_CHART_DIGEST
  image=$KAGENT_CONTROLLER_IMAGE
  overlay=$root/infra/kubernetes/kagent-pilot/values/kagent-runtime-fix-overlay.yaml
  renderer=(--post-renderer "$root/infra/kubernetes/kagent-pilot/scripts/kagent_runtime_post_renderer.py")
  candidate_renderer=("${renderer[@]}")
  values_args=(-f "$overlay")
  lookup=()
  pinned=$(python3 -c 'import sys,yaml; i=yaml.safe_load(open(sys.argv[1]))["controller"]["image"]; print(i["registry"] + "/" + i["repository"] + ":" + i["tag"])' "$overlay")
  [[ "$pinned" == "$image" ]] || die "the overlay pins $pinned, not $image"
elif [[ $target == kagent-otel ]]; then
  package=P2C-KAGENT-OTEL release=kagent namespace=kagent deployment=kagent-controller container=controller
  chart_ref=$KAGENT_CHART chart_version=$KAGENT_VERSION chart_digest=$KAGENT_CHART_DIGEST
  image=$KAGENT_CONTROLLER_IMAGE   # unchanged: verify proves the controller still runs the PKG-T build
  overlay=$root/infra/kubernetes/kagent-pilot/values/kagent-otel-endpoint-overlay.yaml
  renderer=(--post-renderer "$root/infra/kubernetes/kagent-pilot/scripts/kagent_runtime_post_renderer.py")
  candidate_renderer=("${renderer[@]}")
  values_args=(-f "$overlay")
  lookup=()
  otel_endpoint=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["otel"]["tracing"]["exporter"]["otlp"]["endpoint"])' "$overlay")
else
  package=PKG-T-SUBSTRATE release=substrate namespace=ate-system deployment=ate-api-server-deployment container=ate-api-server
  chart_ref=$SUBSTRATE_CHART chart_version=$SUBSTRATE_VERSION chart_digest=$SUBSTRATE_CHART_DIGEST
  image=$SUBSTRATE_ATEAPI_IMAGE
  renderer=(--post-renderer "$root/infra/kubernetes/kagent-pilot/scripts/substrate_resource_post_renderer.py")
  candidate_renderer=("${renderer[@]}" --post-renderer-args "--ateapi-image=$image" --post-renderer-args "--atelet-memory=$SUBSTRATE_ATELET_MEMORY")
  values_args=()
  lookup=(--dry-run=server)   # generated CA/TLS/session pools are read back, not regenerated
  # A later PKG-T step finds earlier parts live: the baseline renders with whatever is live, so
  # identity still proves "chart + values + renderer == live" and the diff shows only the rest.
  live_ateapi=$(K -n ate-system get deployment ate-api-server-deployment -o 'jsonpath={.spec.template.spec.containers[?(@.name=="ate-api-server")].image}' 2>/dev/null || true)
  live_mem=$(K -n ate-system get daemonset atelet -o 'jsonpath={.spec.template.spec.containers[?(@.name=="atelet")].resources.requests.memory}:{.spec.template.spec.containers[?(@.name=="atelet")].resources.limits.memory}' 2>/dev/null || true)
  [[ "$live_ateapi" == "$image" ]] && renderer+=(--post-renderer-args "--ateapi-image=$image")
  [[ "$live_mem" == "$SUBSTRATE_ATELET_MEMORY" ]] && renderer+=(--post-renderer-args "--atelet-memory=$SUBSTRATE_ATELET_MEMORY")
  # An earlier patched build is live (e.g. P2-A replaces the PKG-T build): the baseline renders it, so
  # identity still holds and the diff is exactly that image.
  if [[ "$live_ateapi" != "$image" && "$live_ateapi" =~ ^[a-z0-9.-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+/substrate-ateapi:0\.0\.9-ewallet\.[0-9a-f.]+@sha256:[0-9a-f]{64}$ ]]; then
    renderer+=(--post-renderer-args "--ateapi-image=$live_ateapi")
  fi
fi
[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || die "patched image is not digest-pinned: $image"

if [[ "$action" == cleanup ]]; then
  rm -f "$work"/render-baseline.yaml "$work"/render-candidate.yaml "$work"/manifest-*.yaml "$work"/values-live.yaml "$work"/dry-run.yaml
  # render-original.yaml stays until the package is retired: rollback compares against it
  log "cleanup: private renders, manifests and values removed"
  exit 0
fi

fetch_chart() {
  rm -rf "$work/chart" && mkdir -p "$work/chart"
  "$helm" pull "$chart_ref" --version "$chart_version" --destination "$work/chart" > "$work/chart/pull.txt" 2>&1 \
    || die "helm pull $release failed"
  grep -q "Digest: $chart_digest" "$work/chart/pull.txt" || die "$release chart digest is not $chart_digest"
  chart=$work/chart/$release-$chart_version.tgz
}

running_image_ids() {   # Running pods of the Deployment that are not being deleted (a rollout keeps the old one briefly)
  local selector
  selector=$(K -n "$namespace" get deployment "$deployment" -o json \
    | python3 -c 'import json,sys; print(",".join(f"{k}={v}" for k, v in sorted(json.load(sys.stdin)["spec"]["selector"]["matchLabels"].items())))')
  K -n "$namespace" get pods -l "$selector" --field-selector=status.phase=Running -o json | python3 -c '
import json, sys
name = sys.argv[1]
for pod in json.load(sys.stdin)["items"]:
    if pod["metadata"].get("deletionTimestamp"):
        continue
    for c in pod["status"].get("containerStatuses", []):
        if c["name"] == name:
            print(pod["metadata"]["name"], c.get("imageID", ""), c.get("restartCount", 0), str(c.get("ready", False)).lower())' "$container"
}

preflight() {
  fetch_chart
  local status
  status=$(H status "$release" -n "$namespace" -o json) || die "release $namespace/$release not found"
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["info"]["status"]=="deployed", d["info"]["status"]; print(d["version"])' \
    <<< "$status" > "$work/revision-live" || die "release $release is not in status deployed"
  K -n "$namespace" rollout status "deployment/$deployment" --timeout=120s > /dev/null || die "$deployment is not healthy before the change"
  K -n "$namespace" get deployment "$deployment" -o "jsonpath={.spec.template.spec.containers[?(@.name==\"$container\")].image}" > "$work/image-live"
  running_image_ids > "$work/pods-live.txt"
  H get values "$release" -n "$namespace" -o yaml > "$work/values-live.yaml"
  H get manifest "$release" -n "$namespace" > "$work/manifest-live.yaml"
  H template "$release" "$chart" -n "$namespace" -f "$work/values-live.yaml" "${renderer[@]}" "${lookup[@]}" \
    > "$work/render-baseline.yaml" 2> "$work/render-baseline.err" || die "baseline render failed"
  diff_tool identity "$work/manifest-live.yaml" "$work/render-baseline.yaml" > "$work/identity.txt" 2>&1 \
    || { cat "$work/identity.txt" >&2; die "chart + live values + post-renderer do not reproduce the live release; stop"; }
  log "preflight PASS: $namespace/$release revision $(cat "$work/revision-live"), image $(cat "$work/image-live"), identity PASS"
}

candidate() {
  H template "$release" "$chart" -n "$namespace" -f "$work/values-live.yaml" "${values_args[@]}" "${candidate_renderer[@]}" "${lookup[@]}" \
    > "$work/render-candidate.yaml" 2> "$work/render-candidate.err" || die "candidate render failed"
  if [[ $target == kagent-otel ]]; then
    diff_tool candidate kagent-otel "$work/render-baseline.yaml" "$work/render-candidate.yaml" "$otel_endpoint" > "$work/diff.txt" 2>&1 \
      || { cat "$work/diff.txt" >&2; die "the diff is not the reviewed diff"; }
  elif [[ $target == kagent ]]; then
    diff_tool candidate kagent "$work/render-baseline.yaml" "$work/render-candidate.yaml" "$image" "$KAGENT_SUBSTRATE_CALL_TIMEOUT" > "$work/diff.txt" 2>&1 \
      || { cat "$work/diff.txt" >&2; die "the diff is not the reviewed diff"; }
  else
    diff_tool candidate substrate "$work/render-baseline.yaml" "$work/render-candidate.yaml" "$image" "$SUBSTRATE_ATELET_MEMORY" > "$work/diff.txt" 2>&1 \
      || { cat "$work/diff.txt" >&2; die "the diff is not the reviewed diff"; }
  fi
  log "diff PASS: $(tr -d '\n ' < "$work/diff.txt" | cut -c1-300)"
}

upgrade_args() { echo upgrade "$release" "$chart" -n "$namespace" --reuse-values "${values_args[@]}"; }

server_dry_run() {
  # shellcheck disable=SC2046
  H $(upgrade_args) "${candidate_renderer[@]}" --dry-run=server > "$work/dry-run.yaml" 2> "$work/dry-run.err" \
    || { tail -3 "$work/dry-run.err" >&2; die "server-side dry-run failed; nothing was upgraded"; }
  log "dry-run PASS (server-side)"
}

verify() {
  local want_digest=$1 pods
  K -n "$namespace" rollout status "deployment/$deployment" --timeout=600s > /dev/null || die "$deployment is not rolled out"
  pods=$(running_image_ids)
  [[ -n "$pods" ]] || die "no Running $deployment pod"
  while read -r pod image_id restarts ready; do
    [[ "$image_id" == *"@$want_digest" ]] || die "$pod runs $image_id, not $want_digest"
    [[ "$restarts" == 0 && "$ready" == true ]] || die "$pod restarts=$restarts ready=$ready"
  done <<< "$pods"
  log "verify PASS: $(tr '\n' ';' <<< "$pods")"
}

approval() { [[ "${RUNTIME_FIX_APPROVAL:-}" == "$1" ]] || die "set RUNTIME_FIX_APPROVAL=$1 only under the owner's approval of this exact package"; }

case "$action" in
  preflight) preflight ;;
  diff) preflight; candidate ;;
  dry-run) preflight; candidate; server_dry_run ;;
  upgrade)
    approval "$package:$KUBE_CONTEXT"
    preflight; candidate; server_dry_run
    cp "$work/revision-live" "$work/revision-before-upgrade"
    cp "$work/image-live" "$work/image-before-upgrade"
    cp "$work/pods-live.txt" "$work/pods-before-upgrade.txt"
    # The release state before the first PKG-T step: rollback returns there, not to a partial step.
    if [[ ! -s "$work/revision-original" ]]; then
      cp "$work/revision-live" "$work/revision-original"; cp "$work/render-baseline.yaml" "$work/render-original.yaml"
      cp "$work/pods-live.txt" "$work/pods-original.txt"
    fi
    log "upgrade: $(upgrade_args) (from revision $(cat "$work/revision-before-upgrade"), image $(cat "$work/image-before-upgrade"))"
    # shellcheck disable=SC2046
    H $(upgrade_args) "${candidate_renderer[@]}" --atomic --wait --timeout 10m > "$work/upgrade.txt" 2>&1 \
      || { tail -3 "$work/upgrade.txt" >&2; die "helm upgrade failed (--atomic rolled it back)"; }
    verify "${image##*@}" ;;
  verify) verify "${image##*@}" ;;
  rollback)
    approval "$package-ROLLBACK:$KUBE_CONTEXT"
    [[ -s "$work/revision-original" && -s "$work/render-original.yaml" && -s "$work/pods-original.txt" ]] \
      || die "no recorded original revision, baseline or image in $work"
    before=$(cat "$work/revision-original")
    before_digest=$(awk 'NR==1{print $2}' "$work/pods-original.txt"); before_digest=${before_digest##*@}
    H rollback "$release" "$before" -n "$namespace" --wait --timeout 10m > "$work/rollback.txt" 2>&1 \
      || { tail -3 "$work/rollback.txt" >&2; die "helm rollback failed"; }
    verify "$before_digest"
    H get manifest "$release" -n "$namespace" > "$work/manifest-after-rollback.yaml"
    diff_tool identity "$work/manifest-after-rollback.yaml" "$work/render-original.yaml" > "$work/identity-after-rollback.txt" 2>&1 \
      || { cat "$work/identity-after-rollback.txt" >&2; die "rolled-back release differs from the pre-upgrade manifest"; }
    log "rollback PASS: revision $before restored, manifest identical to the pre-upgrade release" ;;
esac
