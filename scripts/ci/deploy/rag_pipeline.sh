#!/usr/bin/env bash
# Row 33 deploy handler: roll the suspended rag-ingestion CronJob in
# ewallet-cicd to the Cloud Build digest, run one smoke Job from it against the
# namespace's ephemeral Redis, verify it against jenkins/cd/rag-pipeline.contract.json,
# then remove the temporary Job and Redis. On a failed verdict the previous
# CronJob (if any) is restored. Production Redis and Airflow are never touched.
set -euo pipefail

image="" git_sha="" evidence=""
while (($#)); do
  case "$1" in
    --image) image=$2; shift 2 ;;
    --git-sha) git_sha=$2; shift 2 ;;
    --evidence-dir) evidence=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ $image =~ ^[a-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] || { echo "image must be a digest reference" >&2; exit 2; }
[[ $git_sha =~ ^[0-9a-f]{40}$ ]] || { echo "git SHA must be a full SHA" >&2; exit 2; }
[[ -n $evidence ]] || { echo "--evidence-dir is required" >&2; exit 2; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "$root"
namespace=ewallet-cicd
short=${git_sha:0:12}
job="rag-cd-${short}-${BUILD_NUMBER:-local}"
mkdir -p "$evidence"
k8s() {
  uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.k8s_api \
    ${K8S_API:+--api "$K8S_API"} --allow-namespace "$namespace" "$@"
}

previous="$evidence/previous-cronjob.json"
if k8s get --namespace "$namespace" --kind CronJob --name rag-ingestion > "$previous" 2>/dev/null; then
  echo "previous image: $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"])' "$previous")"
else
  rm -f "$previous"
  echo "previous image: none (first deployment)"
fi

rendered="$evidence/rendered.yaml"
sed -e "s|__RAG_IMAGE__|$image|g" -e "s|__RELEASE_SHA__|$git_sha|g" -e "s|__RELEASE_SHORT__|$short|g" \
  infra/kubernetes/cicd-runtime/rag/rag-ingestion.yaml > "$rendered"
! grep -q '__[A-Z_]*__' "$rendered"
k8s apply -f "$rendered" | tee "$evidence/apply.txt"
k8s wait-ready --namespace "$namespace" --kind Deployment --name rag-cd-redis --timeout 300

cleanup() {
  k8s delete --namespace "$namespace" --kind Job --name "$job" || true
  k8s delete --namespace "$namespace" --kind Deployment --name rag-cd-redis || true
  k8s delete --namespace "$namespace" --kind Service --name rag-cd-redis || true
}
trap cleanup EXIT

k8s job-from-cronjob --namespace "$namespace" --cronjob rag-ingestion --job "$job" \
  --label "ewallet.io/git-sha=$git_sha" | tee "$evidence/job.txt"
set +e
k8s wait-job --namespace "$namespace" --job "$job" --timeout 900 | tee "$evidence/job-status.json"
job_rc=${PIPESTATUS[0]}
set -e
k8s logs --namespace "$namespace" --job "$job" > "$evidence/smoke.log"
set +e
uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.verify_rag_smoke \
  --log "$evidence/smoke.log" --release-sha "$git_sha" --output "$evidence/verdict.json"
verify_rc=$?
set -e
if ((job_rc != 0 || verify_rc != 0)); then
  echo "RAG CD verification failed (job=$job_rc verify=$verify_rc); rolling back" >&2
  if [[ -f $previous ]]; then
    python3 - "$previous" "$evidence/rollback.json" <<'PY'
import json, sys
item = json.load(open(sys.argv[1]))
meta = item["metadata"]
json.dump({"apiVersion": "batch/v1", "kind": "CronJob",
           "metadata": {"name": meta["name"], "namespace": meta["namespace"],
                        "labels": meta.get("labels", {}), "annotations": {
                            k: v for k, v in meta.get("annotations", {}).items() if k.startswith("ewallet.io/")}},
           "spec": item["spec"]}, open(sys.argv[2], "w"))
PY
    k8s apply -f "$evidence/rollback.json" | tee "$evidence/rollback.txt"
  else
    k8s delete --namespace "$namespace" --kind CronJob --name rag-ingestion | tee "$evidence/rollback.txt"
  fi
  exit 1
fi
echo "RAG CD PASS: CronJob rag-ingestion -> $image"
