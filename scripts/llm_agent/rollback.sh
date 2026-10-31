#!/usr/bin/env bash
# Jenkins post-failure rollback: return the release to its last deployed
# revision (helm --atomic already rolls back a failed upgrade; this covers a
# failure after a successful upgrade, e.g. a failed smoke).
# usage: rollback.sh <dev|gcp|gke-cpu> <evidence-dir>
# Env: KUBE_CONTEXT (required), RELEASE (default llm-agent).
set -euo pipefail
environment=$1 evidence=$2
[[ "$environment" =~ ^(dev|gcp|gke-cpu)$ ]] || { echo "environment must be dev, gcp or gke-cpu" >&2; exit 2; }
[ -n "${KUBE_CONTEXT:-}" ] || { echo "KUBE_CONTEXT is required" >&2; exit 2; }
release=${RELEASE:-llm-agent}
mkdir -p "$evidence"
history=$(helm history "$release" --kube-context "$KUBE_CONTEXT" --namespace default -o json)
echo "$history" > "$evidence/rollback-history-before.json"
target=$(python3 -c '
import json, sys
revisions = sorted(json.loads(sys.argv[1]), key=lambda r: r["revision"])
if not revisions:
    sys.exit("release has no history")
current = revisions[-1]
earlier = [r for r in revisions[:-1] if r["status"] in ("deployed", "superseded")]
if not earlier:
    print("")
else:
    print(earlier[-1]["revision"])
' "$history")
if [ -z "$target" ]; then
  echo "no earlier revision to roll back to" | tee "$evidence/rollback.txt"
  exit 1
fi
helm rollback "$release" "$target" --kube-context "$KUBE_CONTEXT" --namespace default --wait --timeout 10m \
  | tee "$evidence/rollback.txt"
