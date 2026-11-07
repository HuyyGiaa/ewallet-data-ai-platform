#!/usr/bin/env bash
# Build (never push) the GPU vLLM image holding both pinned models.
# usage: build_vllm_gpu_image.sh <model-0.6b-dir> <model-1.7b-dir> <registry/repo> <git-sha>
# Every model file must match its SHA256SUMS before anything is built. Pushing goes
# through scripts/llm_agent/scan_and_push.sh and needs owner approval.
set -euo pipefail
small=$1 large=$2 repo=$3 git_sha=$4
root=$(cd "$(dirname "$0")/../.." && pwd)
recipe=$root/infra/docker/vllm-gpu-model
[[ "$git_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "git sha must be 40 hex characters" >&2; exit 2; }
(cd "$small" && sha256sum --check --strict --quiet "$recipe/SHA256SUMS-0.6b")
(cd "$large" && sha256sum --check --strict --quiet "$recipe/SHA256SUMS-1.7b")
context=$(mktemp -d); trap 'rm -rf "$context"' EXIT
stage() {  # <model-dir> <sums> <target>
  while read -r _ file; do
    mkdir -p "$context/$3/$(dirname "$file")"; cp "$1/$file" "$context/$3/$file"
  done < "$2"
}
stage "$small" "$recipe/SHA256SUMS-0.6b" model-0.6b
stage "$large" "$recipe/SHA256SUMS-1.7b" model-1.7b
cp "$recipe/SHA256SUMS-0.6b" "$recipe/SHA256SUMS-1.7b" "$context/"
docker build -f "$recipe/Dockerfile" -t "$repo/vllm-gpu-model:$git_sha" "$context"
