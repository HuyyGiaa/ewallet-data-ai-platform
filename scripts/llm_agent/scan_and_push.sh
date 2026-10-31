#!/usr/bin/env bash
# Jenkins stage "Scan and push Artifact Registry digests".
# usage: scan_and_push.sh <image-digests.json>
# SCAN_ONLY=1 runs the same gate and writes the reports without pushing.
# Fails on CRITICAL/HIGH fixable vulnerabilities or secrets (Trivy, pinned by digest),
# pushes, and records the immutable repo@sha256 reference of every image.
set -euo pipefail
digests=$1
trivy_image=docker.io/aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa
root=$(cd "$(dirname "$0")/../.." && pwd)
report_dir=$(dirname "$digests")/scans
mkdir -p "$report_dir"
for image in $(python3 -c 'import json,sys; print(" ".join(sorted(json.load(open(sys.argv[1]))["images"])))' "$digests"); do
  ref=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["images"][sys.argv[2]]["tag_ref"])' "$digests" "$image")
  # Per-image secret config (exclude-blocks only, reviewed): infra/docker/<image>/trivy-secret.yaml.
  mounts=(-v /var/run/docker.sock:/var/run/docker.sock -v "$report_dir:/out")
  config=()
  if [ -f "$root/infra/docker/$image/trivy-secret.yaml" ]; then
    mounts+=(-v "$root/infra/docker/$image/trivy-secret.yaml:/config/trivy-secret.yaml:ro")
    config=(--secret-config /config/trivy-secret.yaml)
  fi
  docker run --rm "${mounts[@]}" "$trivy_image" \
    image --scanners vuln,secret --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed "${config[@]}" \
    --format json --output "/out/$image.json" "$ref"
  if [ "${SCAN_ONLY:-0}" = 1 ]; then
    echo "$image: gate passed, not pushed (SCAN_ONLY=1)"
    continue
  fi
  docker push "$ref"
  digest_ref=$(docker image inspect "$ref" --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep -m1 "^${ref%:*}@sha256:")
  [[ "$digest_ref" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "no pushed digest for $ref" >&2; exit 1; }
  python3 - "$digests" "$image" "$digest_ref" <<'PY'
import json, sys
path, image, digest_ref = sys.argv[1:]
data = json.load(open(path))
data["images"][image]["digest_ref"] = digest_ref
json.dump(data, open(path, "w"), indent=1, sort_keys=True)
PY
done
cat "$digests"
