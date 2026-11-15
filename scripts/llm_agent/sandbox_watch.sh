#!/usr/bin/env bash
# Sandbox watch packages: add ewallet-sandbox to an existing controller's watch scope.
#   PKG-K  kagent  release kagent/kagent (chart 0.10.3, pilot post-renderer)
#   PKG-W  keda    release keda/keda     (chart 2.21.0)
# The two are separate packages with separate approvals; this script never touches both.
#
# usage: sandbox_watch.sh <kagent|keda> <action> <work-dir>
#   preflight  [READ-ONLY]  pinned chart digest, release deployed, live values/manifest saved
#                           (mode 600), identity: chart + live values (+ post-renderer) == live
#                           manifest, ewallet-sandbox exists
#   diff       [READ-ONLY]  preflight, then the overlay's exact object diff (sandbox_watch_diff)
#   dry-run    [READ-ONLY]  diff, then helm upgrade --dry-run=server
#   upgrade    [MUTATES]    WATCH_APPROVAL=<PKG-K|PKG-W>:<context>; diff and a server-side dry-run in the
#                           same execution, then helm upgrade --atomic; any failed gate stops before it
#   verify     [READ-ONLY]  watch value, rollouts, RBAC in ewallet-sandbox, controller readback
#   rollback   [MUTATES]    WATCH_APPROVAL=<PKG-K|PKG-W>-ROLLBACK:<context>; helm rollback to the
#                           revision recorded by upgrade, then identity against that revision
#
# Env: KUBE_CONTEXT (required; the kubeconfig default is never used), KUBECONFIG, HELM, KUBECTL.
set -euo pipefail
target=${1:-} action=${2:-} work=${3:-}
die() { echo "sandbox_watch: FAIL: $*" >&2; exit 1; }
case "$target" in kagent|keda) ;; *) die "usage: sandbox_watch.sh <kagent|keda> <action> <work-dir>";; esac
case "$action" in preflight|diff|dry-run|upgrade|verify|rollback) ;; *) die "unknown action '$action'";; esac
[[ -n "$work" ]] || die "work directory required"
[[ -n "${KUBE_CONTEXT:-}" ]] || die "KUBE_CONTEXT is required"
root=$(cd "$(dirname "$0")/../.." && pwd)
helm=${HELM:-helm} kubectl=${KUBECTL:-kubectl}
umask 077
mkdir -p "$work/$target"
work=$(cd "$work" && pwd)/$target
H() { "$helm" --kube-context "$KUBE_CONTEXT" "$@"; }
K() { "$kubectl" --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
log() { printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$target" "$*" | tee -a "$work/package.log"; }

if [[ $target == kagent ]]; then
  package=PKG-K release=kagent namespace=kagent
  overlay=$root/infra/kubernetes/kagent-pilot/values/kagent-sandbox-watch-overlay.yaml
  renderer=(--post-renderer "$root/infra/kubernetes/kagent-pilot/scripts/kagent_runtime_post_renderer.py")
  # shellcheck source=../../infra/kubernetes/kagent-pilot/versions.env
  . "$root/infra/kubernetes/kagent-pilot/versions.env"
  expected_watch=ewallet-agents,kagent-pilot,ewallet-sandbox
else
  package=PKG-W release=keda namespace=keda
  overlay=$root/infra/helm/keda/values-sandbox-watch.yaml
  renderer=()
  # shellcheck source=../../infra/helm/keda/versions.env
  . "$root/infra/helm/keda/versions.env"
  expected_watch=ewallet-agents,ml-platform,ewallet-sandbox
fi

fetch_chart() {
  rm -rf "$work/chart" && mkdir -p "$work/chart"
  if [[ $target == kagent ]]; then
    "$helm" pull "$KAGENT_CHART" --version "$KAGENT_VERSION" --destination "$work/chart" > "$work/chart/pull.txt" 2>&1 \
      || die "helm pull kagent failed: $(tail -1 "$work/chart/pull.txt")"
    grep -q "Digest: $KAGENT_CHART_DIGEST" "$work/chart/pull.txt" || die "kagent chart digest is not $KAGENT_CHART_DIGEST"
    chart=$work/chart/kagent-$KAGENT_VERSION.tgz
  else
    "$helm" pull keda --repo "$KEDA_CHART_REPO" --version "$KEDA_VERSION" --destination "$work/chart" > "$work/chart/pull.txt" 2>&1 \
      || die "helm pull keda failed"
    chart=$work/chart/keda-$KEDA_VERSION.tgz
    [[ "$(sha256sum "$chart" | cut -d' ' -f1)" == "$KEDA_CHART_TGZ_SHA256" ]] || die "keda chart hash is not $KEDA_CHART_TGZ_SHA256"
  fi
}

diff_tool() { (cd "$root" && python3 -m ops.llm_agent.sandbox_watch_diff "$@"); }

preflight() {
  fetch_chart
  local status
  status=$(H status "$release" -n "$namespace" -o json) || die "release $namespace/$release not found"
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["info"]["status"]=="deployed", d["info"]["status"]; print(d["version"])' \
    <<< "$status" > "$work/revision-live" || die "release $release is not in status deployed"
  H get values "$release" -n "$namespace" -o yaml > "$work/values-live.yaml"
  H get manifest "$release" -n "$namespace" > "$work/manifest-live.yaml"
  "$helm" template "$release" "$chart" -n "$namespace" -f "$work/values-live.yaml" "${renderer[@]}" > "$work/render-baseline.yaml" \
    || die "baseline render failed"
  diff_tool identity "$work/manifest-live.yaml" "$work/render-baseline.yaml" > "$work/identity.txt" 2>&1 \
    || { cat "$work/identity.txt" >&2; die "chart + live values do not reproduce the live release; stop"; }
  K get namespace ewallet-sandbox -o name > /dev/null 2>&1 || die "namespace ewallet-sandbox does not exist (create it first)"
  log "preflight PASS: $namespace/$release revision $(cat "$work/revision-live"), identity PASS"
}

candidate() {
  "$helm" template "$release" "$chart" -n "$namespace" -f "$work/values-live.yaml" -f "$overlay" "${renderer[@]}" \
    > "$work/render-candidate.yaml" || die "candidate render failed"
  diff_tool candidate "$target" "$work/render-baseline.yaml" "$work/render-candidate.yaml" > "$work/diff.txt" 2>&1 \
    || { cat "$work/diff.txt" >&2; die "overlay diff is not the reviewed diff"; }
  log "diff PASS: $(tr -d '\n ' < "$work/diff.txt" | cut -c1-400)"
}

upgrade_args() { echo upgrade "$release" "$chart" -n "$namespace" --reuse-values -f "$overlay"; }

server_dry_run() {   # the exact upgrade, validated by the API server; fails closed
  # shellcheck disable=SC2046
  H $(upgrade_args) "${renderer[@]}" --dry-run=server > "$work/dry-run.yaml" 2> "$work/dry-run.err" \
    || { tail -5 "$work/dry-run.err" >&2; die "server-side dry-run failed; nothing was upgraded"; }
  log "dry-run PASS (server-side)"
}

watch_value() {
  if [[ $target == kagent ]]; then
    K -n kagent get configmap kagent-controller -o 'jsonpath={.data.WATCH_NAMESPACES}'
  else
    K -n keda get deployment keda-operator -o 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="WATCH_NAMESPACE")].value}'
  fi
}

deployments() { if [[ $target == kagent ]]; then echo kagent-controller; else echo keda-operator keda-operator-metrics-apiserver keda-admission-webhooks; fi; }

verify() {
  local expected=$1 got deployment
  got=$(watch_value)
  [[ "$got" == "$expected" ]] || die "watch scope is '$got', expected '$expected'"
  for deployment in $(deployments); do
    K -n "$namespace" rollout status "deployment/$deployment" --timeout=300s > /dev/null || die "$deployment is not rolled out"
  done
  local rbac
  rbac=$(K -n ewallet-sandbox get role,rolebinding -l "app.kubernetes.io/instance=$release" -o name 2>/dev/null | sort | tr '\n' ' ')
  if [[ "$expected" == *ewallet-sandbox* ]]; then
    [[ -n "$rbac" ]] || die "no $release RBAC in ewallet-sandbox"
    if [[ $target == kagent ]]; then
      # The controller logs the namespaces its cache watches at start-up. Read the newest Running
      # pod of the Deployment's own selector: right after a rollout the old pod may still exist.
      local selector pod
      selector=$(K -n kagent get deployment kagent-controller -o json \
        | python3 -c 'import json,sys; print(",".join(f"{k}={v}" for k, v in sorted(json.load(sys.stdin)["spec"]["selector"]["matchLabels"].items())))')
      pod=$(K -n kagent get pods -l "$selector" --field-selector=status.phase=Running \
        --sort-by=.metadata.creationTimestamp -o 'jsonpath={.items[-1:].metadata.name}')
      [[ -n "$pod" ]] || die "no Running kagent-controller pod"
      K -n kagent logs "$pod" --tail=400 | grep 'Watching specific namespaces' | tail -1 | grep -q ewallet-sandbox \
        || die "kagent-controller pod $pod start-up log does not list ewallet-sandbox"
      log "controller pod $pod watches: $(K -n kagent logs "$pod" --tail=400 | grep 'Watching specific namespaces' | tail -1 | grep -o '"namespaces":\[[^]]*\]')"
      local agents
      agents=$(K -n ewallet-sandbox get sandboxagents.kagent.dev -o 'jsonpath={range .items[*]}{.metadata.name}={.status.conditions[?(@.type=="Accepted")].status}{" "}{end}')
      log "SandboxAgents in ewallet-sandbox: ${agents:-none yet (AGS-01 checks reconciliation)}"
      [[ "$agents" != *"=False"* ]] || die "a SandboxAgent in ewallet-sandbox is not Accepted: $agents"
    else
      local objects
      objects=$(K get scaledobjects.keda.sh -A -o 'jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}={.status.conditions[?(@.type=="Ready")].status}{" "}{end}')
      log "ScaledObjects: ${objects:-none}"
      [[ "$objects" != *"=False"* ]] || die "a ScaledObject is not Ready after the change: $objects"
    fi
  else
    [[ -z "$rbac" ]] || die "$release RBAC still present in ewallet-sandbox: $rbac"
  fi
  log "verify PASS: watch scope $got; RBAC in ewallet-sandbox: ${rbac:-none}"
}

approval() {
  local want=$1
  [[ "${WATCH_APPROVAL:-}" == "$want" ]] || die "set WATCH_APPROVAL=$want only after the owner approves this exact package"
}

case "$action" in
  preflight) preflight ;;
  diff) preflight; candidate ;;
  dry-run)
    preflight; candidate; server_dry_run ;;
  upgrade)
    approval "$package:$KUBE_CONTEXT"
    preflight; candidate; server_dry_run
    cp "$work/revision-live" "$work/revision-before-upgrade"
    log "upgrade: $(upgrade_args) (from revision $(cat "$work/revision-before-upgrade"))"
    # shellcheck disable=SC2046
    H $(upgrade_args) "${renderer[@]}" --atomic --wait --timeout 10m > "$work/upgrade.txt" 2>&1 \
      || { tail -5 "$work/upgrade.txt" >&2; die "helm upgrade failed (--atomic rolled it back)"; }
    verify "$expected_watch" ;;
  verify) verify "$expected_watch" ;;
  rollback)
    approval "$package-ROLLBACK:$KUBE_CONTEXT"
    [[ -s "$work/revision-before-upgrade" ]] || die "no recorded pre-upgrade revision in $work"
    before=$(cat "$work/revision-before-upgrade")
    original=${expected_watch%,ewallet-sandbox}
    H rollback "$release" "$before" -n "$namespace" --wait --timeout 10m > "$work/rollback.txt" 2>&1 \
      || { tail -5 "$work/rollback.txt" >&2; die "helm rollback failed"; }
    verify "$original"
    H get manifest "$release" -n "$namespace" > "$work/manifest-after-rollback.yaml"
    diff_tool identity "$work/manifest-after-rollback.yaml" "$work/render-baseline.yaml" > "$work/identity-after-rollback.txt" 2>&1 \
      || { cat "$work/identity-after-rollback.txt" >&2; die "rolled-back release differs from the pre-upgrade manifest"; }
    log "rollback PASS: revision $before restored, manifest identical to the pre-upgrade release" ;;
esac
