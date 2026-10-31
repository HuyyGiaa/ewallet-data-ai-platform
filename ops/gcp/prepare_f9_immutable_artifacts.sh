#!/usr/bin/env bash
set -euo pipefail

: "${EXPECTED_REVIEWED_GIT_SHA:?Supply the externally reviewed source SHA}"
: "${PROJECT_ID:?}"
: "${REGION:?}"
: "${F9_IMAGE_REPOSITORY:?Example: REGION-docker.pkg.dev/PROJECT/repo/f9-kfp-runtime}"
: "${PIPELINE_PACKAGE:?}"
: "${F9_ARTIFACT_EVIDENCE:?}"

test "$(git rev-parse HEAD)" = "$EXPECTED_REVIEWED_GIT_SHA"
test -z "$(git status --porcelain)"
F9_BUILD_TAG="${F9_IMAGE_REPOSITORY}:git-${EXPECTED_REVIEWED_GIT_SHA}"

# The following cloud build/push requires separate runtime authorization.
gcloud builds submit . \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --config=infra/kubernetes/kfp/image/cloudbuild.yaml \
  --substitutions="_IMAGE=${F9_BUILD_TAG}"

F9_IMAGE_DIGEST="$(gcloud artifacts docker images describe "$F9_BUILD_TAG" \
  --project="$PROJECT_ID" --format='value(image_summary.digest)')"
test -n "$F9_IMAGE_DIGEST"
F9_RUNTIME_IMAGE="${F9_IMAGE_REPOSITORY}@${F9_IMAGE_DIGEST}"

uv run --locked --project ai_platform/ml/pipelines \
  python -m ai_platform.ml.pipelines.compile_pipeline \
  --runtime-image "$F9_RUNTIME_IMAGE" \
  --output "$PIPELINE_PACKAGE"

export F9_RUNTIME_IMAGE F9_IMAGE_DIGEST
python - "$F9_ARTIFACT_EVIDENCE" "$PIPELINE_PACKAGE" "$EXPECTED_REVIEWED_GIT_SHA" <<'PY'
import hashlib, json, os, pathlib, sys
evidence_path = pathlib.Path(sys.argv[1])
yaml_path = pathlib.Path(sys.argv[2])
source_sha = sys.argv[3]
payload = yaml_path.read_bytes()
evidence = {
    "schema_version": "f9-immutable-artifact-evidence-v1",
    "status": "BUILT_AND_DIGEST_VERIFIED",
    "source_git_sha": source_sha,
    "image_uri": os.environ["F9_RUNTIME_IMAGE"],
    "image_digest": os.environ["F9_IMAGE_DIGEST"],
    "compiled_yaml_sha256": hashlib.sha256(payload).hexdigest(),
}
evidence_path.parent.mkdir(parents=True, exist_ok=True)
evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
PY

python -m ops.gcp.validate_f9_artifacts \
  --evidence "$F9_ARTIFACT_EVIDENCE" \
  --compiled-yaml "$PIPELINE_PACKAGE" \
  --expected-source-git-sha "$EXPECTED_REVIEWED_GIT_SHA"
