#!/usr/bin/env bash
# Jenkins stage "Scan and push Artifact Registry digests".
# usage: scan_and_push.sh <image-digests.json>
# Fails on CRITICAL/HIGH fixable vulnerabilities (Trivy, pinned by digest),
# pushes, and records the immutable repo@sha256 reference of every image.
set -euo pipefail
digests=$1
trivy_image=docker.io/aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa
report_dir=$(dirname "$digests")/scans
mkdir -p "$report_dir"
for image in $(python3 -c 'import json,sys; print(" ".join(sorted(json.load(open(sys.argv[1]))["images"])))' "$digests"); do
  ref=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["images"][sys.argv[2]]["tag_ref"])' "$digests" "$image")
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$report_dir:/out" "$trivy_image" \
    image --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed --format json --output "/out/$image.json" "$ref"
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
