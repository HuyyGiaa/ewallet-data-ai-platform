#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
plan=""
execute=0
while (($#)); do
  case "$1" in
    --plan)
      (($# >= 2)) || { echo "--plan requires a path" >&2; exit 2; }
      plan=$2
      shift 2
      ;;
    --execute)
      execute=1
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
[[ -n "$plan" ]] || { echo "--plan is required" >&2; exit 2; }

cd "$root"
python3 -m scripts.ci.release_policy --root "$root" verify --plan "$plan"

mapfile -t metadata < <(python3 - "$plan" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("environment", "kube_context", "image_manifest_path", "evidence_dir", "git_sha"):
    print(plan[key])
PY
)
environment=${metadata[0]}
kube_context=${metadata[1]}
image_manifest=${metadata[2]}
evidence_dir=${metadata[3]}
git_sha=${metadata[4]}

while IFS=$'\t' read -r component handler digest_ref; do
  # Bounded CD handlers (scripts/ci/deploy/) take the release SHA and their own evidence directory.
  handler_args=()
  if [[ $handler == scripts/ci/deploy/* ]]; then
    handler_args=(--image "$digest_ref" --git-sha "$git_sha" --evidence-dir "$evidence_dir/$component")
  fi
  if ((!execute)); then
    printf 'DRY RUN: %s -> %s (%s)%s\n' "$component" "$handler" "$digest_ref" \
      "$( ((${#handler_args[@]})) && printf ' %s' "${handler_args[@]:2}")"
    continue
  fi
  command_log=${DEPLOY_COMMAND_LOG:-"$evidence_dir/deploy-commands.log"}
  mkdir -p "$(dirname "$command_log")"
  printf '%s\t%s\t%s\n' "$component" "$handler" "$digest_ref" >> "$command_log"
  case "$handler" in
    scripts/mlops/deploy_kfp.sh)
      "$root/$handler" --execute
      ;;
    scripts/mlops/deploy_mlflow.sh)
      MLFLOW_IMAGE="$digest_ref" "$root/$handler" --execute
      ;;
    scripts/llm_agent/deploy_atomic.sh)
      KUBE_CONTEXT="$kube_context" "$root/$handler" \
        "$environment" "$image_manifest" "$evidence_dir"
      ;;
    scripts/ci/deploy/rag_pipeline.sh|scripts/ci/deploy/mcp_agents.sh|scripts/ci/deploy/agents.sh|scripts/ci/deploy/streaming.sh)
      "$root/$handler" "${handler_args[@]}"
      ;;
    *)
      echo "release handler is not allowlisted: $handler" >&2
      exit 2
      ;;
  esac
done < <(python3 - "$plan" <<'PY'
import json, sys
for unit in json.load(open(sys.argv[1], encoding="utf-8"))["units"]:
    print("\t".join((unit["component"], unit["handler"], unit["digest_ref"])))
PY
)
