#!/usr/bin/env bash
# B' compatibility patch for the Substrate 0.0.9 WorkerPool CRD: adds spec.scaleSelector and
# points /scale's labelSelectorPath at it, so the HPA that KEDA creates has a selector.
# CLUSTER-SCOPED and Helm-managed (release substrate-crds): every mutating command needs
#   CRD_PATCH_APPROVED=yes and --context, and a GKE context additionally needs the owner's
#   separate approval for that cluster. A substrate-crds upgrade reverts the patch.
#
#   crd_patch.sh backup  CTX DIR   [READ-ONLY] save the live CRD and its spec digest
#   crd_patch.sh diff    CTX       [READ-ONLY] show the patched spec against the live one
#   crd_patch.sh dry-run CTX       [READ-ONLY] server-side dry run of the patch
#   crd_patch.sh apply   CTX       [MUTATES]   apply the patch (refuses if already patched)
#   crd_patch.sh verify  CTX POOL NS [READ-ONLY] /scale of one WorkerPool shows the selector
#   crd_patch.sh revert  CTX DIR   [MUTATES]   restore the backed-up spec (refuses while a
#                                              WorkerPool still uses spec.scaleSelector)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
patch="$here/workerpool-selector.patch.json"
crd=workerpools.ate.dev
command=${1:-} ctx=${2:-}
die() { echo "crd_patch: $*" >&2; exit 1; }
[[ -n "$ctx" ]] || die "usage: crd_patch.sh <backup|diff|dry-run|apply|verify|revert> <context> ..."
k() { kubectl --context "$ctx" --request-timeout=60s "$@"; }
spec_digest() { python3 -c 'import json,sys; print(__import__("hashlib").sha256(json.dumps(json.load(sys.stdin)["spec"], sort_keys=True).encode()).hexdigest())'; }
patched() { k get crd "$crd" -o jsonpath='{.spec.versions[0].subresources.scale.labelSelectorPath}'; }
approved() {
  [[ "${CRD_PATCH_APPROVED:-}" == yes ]] || die "set CRD_PATCH_APPROVED=yes (cluster-scoped change)"
  # A GKE cluster needs its own, named approval on top: the kind experiment never covers it.
  if [[ "$ctx" == gke_* ]]; then
    [[ "${CRD_PATCH_GKE_APPROVED:-}" == "$ctx" ]] || die "GKE context: set CRD_PATCH_GKE_APPROVED=$ctx after the owner approves"
  fi
}
case "$command" in
  backup)
    dir=${3:?backup directory}; mkdir -p "$dir"
    [[ ! -e "$dir/workerpools.ate.dev.json" ]] || die "$dir already holds a backup"
    k get crd "$crd" -o json > "$dir/workerpools.ate.dev.json"
    spec_digest < "$dir/workerpools.ate.dev.json" | tee "$dir/workerpools.ate.dev.spec.sha256"
    k get crd "$crd" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}{" "}{.metadata.annotations.meta\.helm\.sh/release-name}{"\n"}' \
      | tee "$dir/ownership.txt" ;;
  diff)
    k get crd "$crd" -o json > /tmp/crd-live.$$.json
    python3 - /tmp/crd-live.$$.json "$patch" <<'PY'
import copy, json, sys
live = json.load(open(sys.argv[1])); ops = json.load(open(sys.argv[2]))
new = copy.deepcopy(live)
for op in ops:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
    target = new
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    key = int(parts[-1]) if isinstance(target, list) else parts[-1]
    if op["op"] == "test":
        assert target[key] == op["value"], f"test failed at {op['path']}"
    elif op["op"] == "add":
        assert key not in target, f"{op['path']} already present"
        target[key] = op["value"]
print(json.dumps({"scale": new["spec"]["versions"][0]["subresources"]["scale"],
                  "scaleSelector": new["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["scaleSelector"]}, indent=1))
PY
    rm -f /tmp/crd-live.$$.json ;;
  dry-run)
    [[ -z "$(patched)" ]] || die "already patched"
    k patch crd "$crd" --type=json --patch-file "$patch" --dry-run=server -o jsonpath='{.spec.versions[0].subresources.scale}{"\n"}' ;;
  apply)
    approved
    [[ -z "$(patched)" ]] || die "already patched"
    k patch crd "$crd" --type=json --patch-file "$patch" --field-manager=ewallet-substrate-scale
    [[ "$(patched)" == ".spec.scaleSelector" ]] || die "labelSelectorPath not set after the patch"
    echo "patched: labelSelectorPath=.spec.scaleSelector" ;;
  verify)
    pool=${3:?WorkerPool name} ns=${4:?namespace}
    selector=$(k get --raw "/apis/ate.dev/v1alpha1/namespaces/$ns/workerpools/$pool/scale" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"].get("selector",""))')
    [[ "$selector" == "ate.dev/worker-pool=$pool" ]] || die "scale selector is '$selector', expected ate.dev/worker-pool=$pool"
    pods=$(k -n "$ns" get pods -l "$selector" -o name | wc -l)
    echo "PASS /scale selector=$selector matches $pods worker pod(s)" ;;
  revert)
    approved
    dir=${3:?backup directory}
    users=$(k get workerpools -A -o jsonpath='{range .items[?(@.spec.scaleSelector)]}{.metadata.namespace}/{.metadata.name}{" "}{end}')
    [[ -z "$users" ]] || die "WorkerPools still set spec.scaleSelector: $users (remove it, and the ScaledObjects, first)"
    python3 - "$dir/workerpools.ate.dev.json" <<'PY' | k replace -f -
import json, sys
crd = json.load(open(sys.argv[1]))
for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
    crd["metadata"].pop(key, None)
crd.pop("status", None)
print(json.dumps(crd))
PY
    [[ -z "$(patched)" ]] || die "labelSelectorPath still present after revert"
    live=$(k get crd "$crd" -o json | spec_digest)
    [[ "$live" == "$(cat "$dir/workerpools.ate.dev.spec.sha256")" ]] || die "spec digest differs from the backup"
    echo "reverted: spec digest equals the backup" ;;
  *) die "unknown command $command" ;;
esac
