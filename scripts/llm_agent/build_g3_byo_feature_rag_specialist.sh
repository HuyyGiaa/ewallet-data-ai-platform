#!/usr/bin/env bash
# Build the G3 BYO Feature-RAG Specialist from an exact committed revision.
# Usage: build_g3_byo_feature_rag_specialist.sh <full-git-sha> <output-json>
set -euo pipefail

git_sha=${1:?full Git SHA is required}
output=${2:?output JSON path is required}
root=$(cd "$(dirname "$0")/../.." && pwd)

[[ "$git_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "Git SHA must be 40 lowercase hexadecimal characters" >&2; exit 2; }
actual=$(git -C "$root" rev-parse HEAD)
[[ "$actual" = "$git_sha" ]] || { echo "HEAD $actual does not match requested source $git_sha" >&2; exit 2; }
[[ -z "$(git -C "$root" status --porcelain --untracked-files=no)" ]] || {
  echo "Tracked worktree must be clean before an immutable build" >&2
  exit 2
}

image="asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-feature-rag-specialist:$git_sha"
mkdir -p "$(dirname "$output")"
docker build \
  --label "org.opencontainers.image.revision=$git_sha" \
  --label "org.opencontainers.image.source=ewallet-data-ai-platform" \
  --file "$root/infra/docker/g3-byo-coordinator/Dockerfile" \
  --tag "$image" \
  "$root"

# Import the exact production module from the final runtime image. The Agent
# CR overrides only the image CMD argument while preserving `python -m`.
docker run --rm --entrypoint python "$image" -c \
  'from ai_platform.llm_agent.byo_feature_rag_specialist import app; print(app.title)'

python3 - "$output" "$image" "$git_sha" <<'PY'
import json
import pathlib
import sys

path, image, git_sha = sys.argv[1:]
payload = {
    "git_sha": git_sha,
    "images": {
        "g3-byo-feature-rag-specialist": {
            "digest_ref": None,
            "tag_ref": image,
        }
    },
}
pathlib.Path(path).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
PY

docker image inspect "$image" --format '{{json .Config.Labels}}'
cat "$output"
