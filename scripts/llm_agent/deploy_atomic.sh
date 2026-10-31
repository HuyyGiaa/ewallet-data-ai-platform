#!/usr/bin/env bash
# Jenkins stage "Helm atomic deploy, rollout and smoke".
# usage: deploy_atomic.sh <dev|gcp> <image-digests.json> <evidence-dir>
# Env: KUBE_CONTEXT (required, never the kubeconfig default), RELEASE (default
# llm-agent), NAMESPACE (default ewallet-agents).
set -euo pipefail
environment=$1 digests=$2 evidence=$3
[[ "$environment" =~ ^(dev|gcp)$ ]] || { echo "environment must be dev or gcp" >&2; exit 2; }
[ -n "${KUBE_CONTEXT:-}" ] || { echo "KUBE_CONTEXT is required" >&2; exit 2; }
release=${RELEASE:-llm-agent}
namespace=${NAMESPACE:-ewallet-agents}
root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
mkdir -p "$evidence"

args=(upgrade --install "$release" "$chart" -f "$chart/values-$environment.yaml"
      --kube-context "$KUBE_CONTEXT" --namespace default --atomic --wait --timeout 15m)
mcp=$(python3 -c '
import json, re, sys
data = json.load(open(sys.argv[1]))
image = data["images"].get("mcp-tools")
if image:
    ref = image.get("digest_ref") or ""
    if not re.fullmatch(r"[a-z0-9.-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", ref):
        sys.exit("mcp-tools has no pushed immutable digest")
    print(ref, data["git_sha"])
' "$digests")
if [ -n "$mcp" ]; then
  read -r ref git_sha <<< "$mcp"
  args+=(--set "mcpServices.image=$ref" --set "mcpServices.identity.imageDigest=${ref#*@}"
         --set "mcpServices.identity.gitSha=$git_sha")
fi
helm "${args[@]}" | tee "$evidence/helm-upgrade.txt"
helm history "$release" --kube-context "$KUBE_CONTEXT" --namespace default -o json > "$evidence/helm-history.json"
for deployment in $(kubectl --context "$KUBE_CONTEXT" -n "$namespace" get deploy -o name); do
  kubectl --context "$KUBE_CONTEXT" -n "$namespace" rollout status "$deployment" --timeout=10m | tee -a "$evidence/rollout.txt"
done
kubectl --context "$KUBE_CONTEXT" -n "$namespace" get deploy,pods -o wide > "$evidence/workloads.txt"
