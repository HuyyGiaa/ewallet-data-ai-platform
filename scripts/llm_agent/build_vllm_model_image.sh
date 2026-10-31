#!/usr/bin/env bash
# Build (never push) the model-inside-image vLLM CPU image.
# usage: build_vllm_model_image.sh <staged-model-dir> <registry/repo> <git-sha> <digests.json>
# The model dir must match infra/docker/vllm-cpu-model/SHA256SUMS exactly
# before anything is built. Pushing goes through scan_and_push.sh (owner approval).
set -euo pipefail
model=$1 repo=$2 git_sha=$3 out=$4
root=$(cd "$(dirname "$0")/../.." && pwd)
recipe="$root/infra/docker/vllm-cpu-model"
[[ "$git_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "git sha must be 40 hex characters" >&2; exit 2; }
(cd "$model" && sha256sum --check --strict --quiet "$recipe/SHA256SUMS")
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
while read -r _ file; do
  mkdir -p "$context/$(dirname "$file")"
  cp "$model/$file" "$context/$file"
done < "$recipe/SHA256SUMS"
cp "$recipe/SHA256SUMS" "$context/SHA256SUMS"
ref="$repo/vllm-cpu-model:$git_sha"
docker build -f "$recipe/Dockerfile" -t "$ref" "$context"
python3 - "$out" "$ref" "$git_sha" <<'PY'
import json, os, sys
path, ref, sha = sys.argv[1:]
data = json.load(open(path)) if os.path.exists(path) else {"git_sha": sha, "images": {}}
data["images"]["vllm-cpu-model"] = {"tag_ref": ref, "target": "vllm-cpu-model", "components": ["vllm"], "digest_ref": None}
json.dump(data, open(path, "w"), indent=2)
PY
