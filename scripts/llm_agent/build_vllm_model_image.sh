#!/usr/bin/env bash
# Build (never push) the model-inside-image vLLM CPU image.
# usage: build_vllm_model_image.sh <staged-model-dir> <registry/repo> <git-sha> <digests.json>
# The model dir must match infra/docker/vllm-cpu-model/SHA256SUMS exactly
# before anything is built, and the remediation project must pass the uv
# contract (lock --check, sync --locked, run --locked). Env UV: uv binary
# (default: uv on PATH). Pushing goes through scan_and_push.sh (owner approval).
set -euo pipefail
model=$1 repo=$2 git_sha=$3 out=$4
root=$(cd "$(dirname "$0")/../.." && pwd)
recipe="$root/infra/docker/vllm-cpu-model"
[[ "$git_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "git sha must be 40 hex characters" >&2; exit 2; }
(cd "$model" && sha256sum --check --strict --quiet "$recipe/SHA256SUMS")
uv=${UV:-uv}
venv=$(mktemp -d)
context=$(mktemp -d)
trap 'rm -rf "$context" "$venv"' EXIT
(cd "$recipe" && "$uv" lock --check \
   && UV_PROJECT_ENVIRONMENT="$venv" "$uv" sync --locked \
   && UV_PROJECT_ENVIRONMENT="$venv" "$uv" run --locked python -c \
      'from importlib.metadata import version; assert version("certifi") == "2026.10.18"')
while read -r _ file; do
  mkdir -p "$context/model/$(dirname "$file")"
  cp "$model/$file" "$context/model/$file"
done < "$recipe/SHA256SUMS"
cp "$recipe/SHA256SUMS" "$recipe/remediate.py" "$recipe/pyproject.toml" "$recipe/uv.lock" "$context/"
ref="$repo/vllm-cpu-model:$git_sha"
docker build -f "$recipe/Dockerfile" -t "$ref" "$context"
python3 - "$out" "$ref" "$git_sha" <<'PY'
import json, os, sys
path, ref, sha = sys.argv[1:]
data = json.load(open(path)) if os.path.exists(path) else {"git_sha": sha, "images": {}}
data["images"]["vllm-cpu-model"] = {"tag_ref": ref, "target": "vllm-cpu-model", "components": ["vllm"], "digest_ref": None}
json.dump(data, open(path, "w"), indent=2)
PY
