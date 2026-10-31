#!/usr/bin/env bash
# Jenkins stage "Build affected immutable images".
# usage: build_changed_images.sh <changed-components.json> <git-sha> <image-digests.json>
# Builds only images whose components changed, tagged by the exact Git SHA.
# Env: ARTIFACT_REGISTRY=<region>-docker.pkg.dev/<project>/<repository> (required).
set -euo pipefail
components_json=$1 git_sha=$2 out=$3
[[ "$git_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "git sha must be 40 hex characters" >&2; exit 2; }
[[ "${ARTIFACT_REGISTRY:-}" =~ ^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+$ ]] || {
  echo "ARTIFACT_REGISTRY must be <region>-docker.pkg.dev/<project>/<repository>" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)

# image -> Dockerfile target and the components it serves. Agents, gateways,
# llm-d and the registry use pinned upstream images; only these are built.
images=$(python3 - "$components_json" <<'PY'
import json, sys
changed = set(json.load(open(sys.argv[1]))["components"])
catalog = {"mcp-tools": ("mcp-server", ["feature-rag-mcp", "drift-mcp"]),
           "llm-agent-tooling": ("runtime", ["platform-tooling"])}
for image, (target, components) in sorted(catalog.items()):
    if changed & set(components):
        print(image, target, ",".join(sorted(changed & set(components))))
PY
)
echo '{"git_sha": "'"$git_sha"'", "images": {}}' > "$out"
while read -r image target components; do
  [ -n "$image" ] || continue
  ref="$ARTIFACT_REGISTRY/$image:$git_sha"
  docker build --target "$target" -f "$root/infra/docker/llm-agent-tooling/Dockerfile" \
    --label "org.opencontainers.image.revision=$git_sha" --label "org.opencontainers.image.source=ewallet-data-ai-platform" \
    -t "$ref" "$root"
  python3 - "$out" "$image" "$ref" "$target" "$components" <<'PY'
import json, sys
path, image, ref, target, components = sys.argv[1:]
data = json.load(open(path))
data["images"][image] = {"tag_ref": ref, "target": target, "components": components.split(","), "digest_ref": None}
json.dump(data, open(path, "w"), indent=1, sort_keys=True)
PY
done <<< "$images"
cat "$out"
