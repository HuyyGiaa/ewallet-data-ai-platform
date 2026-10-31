# ML-2 bounded Training Snapshot operator runbook

This runbook starts only after the ML-1 source commit is reviewed and checked
out cleanly. It does not rerun G1, G2, G3, republish the Feast Registry, or
re-export the recovered Feast Offline snapshot.

No source-only check in this runbook is cloud SUCCESS. Run the commands in one
Bash process; the fail-fast settings below are mandatory.

## 1. Bind immutable identities and evidence

```bash
set -euo pipefail
export REPO_ROOT="$(pwd)"
export EXPECTED_REVIEWED_GIT_SHA="${EXPECTED_REVIEWED_GIT_SHA:?supplied externally after review}"
export DVC_BIN="${DVC_BIN:?dedicated DVC 3.63.0 executable}"
export PROJECT_ID=ewallet-data-ai-platform-hg26
export REGION=asia-southeast1
export DATA_BUCKET=ewallet-data-ai-platform-hg26-data
export DATASET_VERSION=canonical-100gb-v1
export SILVER_RUN_ID=ab-20261025134342-optimized-silver-r261025181923
export FEATURE_RUN_ID=ab-20261025134342-optimized-feat-r261026025955
export SOURCE_GIT_SHA=bbae48caef7f65fda0c9375a7a49afb2eca5eef4
export FEAST_SNAPSHOT_ID=canonical-training-v1-20261028-101106-recovered
export TRAINING_SNAPSHOT_ID="training-$(date -u +%Y%m%d-%H%M%S)-ml2"
export REGISTRY_GENERATION=1793255806863364
export REGISTRY_SHA256=d3e721969c81db12e6180a4f5f9b7ac2a6db95cdc56034eced2ea4780d439b9a
export ML1_EXECUTION_GIT_SHA="$EXPECTED_REVIEWED_GIT_SHA"
export FEATURE_SELECTION_EVIDENCE=/absolute/path/feature-selection-evidence.json
export FEATURE_SELECTION_DIGEST="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["evidence_digest"])' "$FEATURE_SELECTION_EVIDENCE")"
export PIT_EVIDENCE=/absolute/path/existing-g3-pit-pass.json
export REGISTRY_READBACK_EVIDENCE=/absolute/path/existing-registry-readback.json
export REGISTRY_EXECUTION_GIT_SHA="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["execution_git_sha"])' "$REGISTRY_READBACK_EVIDENCE")"
export TERRAFORM_OUTPUT_JSON=/absolute/path/vetted-terraform-output.json
export LOCAL_ROOT="runtime/cloud-training/$TRAINING_SNAPSHOT_ID"
export SOURCE_DIR="$LOCAL_ROOT/source"
export DATASET_DIR="$LOCAL_ROOT/dataset"
export PUBLICATION_EVIDENCE="$LOCAL_ROOT/publication-evidence.json"
export DVC_DESCRIPTOR="data/training-snapshots/$TRAINING_SNAPSHOT_ID.json"
test "$TRAINING_SNAPSHOT_ID" != "$FEAST_SNAPSHOT_ID"
ops/gcp/ml2_operator_preflight.sh
```

Provision `DVC_BIN` beforehand in a dedicated operator environment from
`ai_platform/ml/dvc-requirements.txt`. The preflight rejects a missing binary or
any version other than 3.63.0. It also rejects a wrong branch, wrong/missing
reviewed SHA, dirty worktree, or missing input evidence before any cloud command.
Never derive `EXPECTED_REVIEWED_GIT_SHA` from the current HEAD.

## 2. Training-source export

The sampling predicate is fixed to
`pmod(xxhash64(event_timestamp_us), 10000) = 0`. The resulting row count is
measured and recorded; it is not assumed to be 37,000.

```bash
export TRAINING_SOURCE_BATCH_ID="train-src-$(date -u +%y%m%d%H%M%S)"
uv run --locked --no-sync --project ai_platform/features/feast python -m ops.gcp.submit_feast_training_export \
  --job training-source \
  --batch-id "$TRAINING_SOURCE_BATCH_ID" \
  --dataset-version "$DATASET_VERSION" \
  --silver-run-id "$SILVER_RUN_ID" \
  --feature-run-id "$FEATURE_RUN_ID" \
  --snapshot-id "$TRAINING_SNAPSHOT_ID" \
  --feast-snapshot-id "$FEAST_SNAPSHOT_ID" \
  --feature-selection-evidence "$FEATURE_SELECTION_EVIDENCE" \
  --pit-evidence "$PIT_EVIDENCE" \
  --trusted-registry-readback-evidence "$REGISTRY_READBACK_EVIDENCE" \
  --expected-registry-uri "gs://$DATA_BUCKET/features/registries/$DATASET_VERSION/$FEAST_SNAPSHOT_ID/registry.db" \
  --expected-registry-generation "$REGISTRY_GENERATION" \
  --expected-registry-sha256 "$REGISTRY_SHA256" \
  --expected-registry-execution-git-sha "$REGISTRY_EXECUTION_GIT_SHA" \
  --sample-modulus 10000 \
  --terraform-output-json "$TERRAFORM_OUTPUT_JSON" \
  --ttl 8h \
  --execute
gcloud dataproc batches describe "$TRAINING_SOURCE_BATCH_ID" --region "$REGION" --format=json
test "$(gcloud dataproc batches describe "$TRAINING_SOURCE_BATCH_ID" --region "$REGION" --format='value(state)')" = SUCCEEDED
test ! -e "$SOURCE_DIR"
gcloud storage rsync --recursive \
  "gs://$DATA_BUCKET/ml/training-staging/$DATASET_VERSION/$TRAINING_SNAPSHOT_ID/source" \
  "$SOURCE_DIR"
test -s "$SOURCE_DIR/manifest.json"
```

## 3. Bounded PIT materialization

Labels stay in the source frame and are never passed into Feast retrieval.
Materialization uses bounded, resumable 1,000-row chunks.

```bash
materialize() {
  uv run --locked --project ai_platform/features/feast python -m ai_platform.ml.training.materialize \
    --environment gcp \
    --dataset-version "$DATASET_VERSION" \
    --source-silver-run-id "$SILVER_RUN_ID" \
    --source-feature-run-id "$FEATURE_RUN_ID" \
    --feast-snapshot-id "$FEAST_SNAPSHOT_ID" \
    --snapshot-id "$TRAINING_SNAPSHOT_ID" \
    --source-git-sha "$SOURCE_GIT_SHA" \
    --feature-selection-digest "$FEATURE_SELECTION_DIGEST" \
    --source "$SOURCE_DIR" \
    --output "$DATASET_DIR" \
    --chunk-size 1000 \
    --max-new-chunks 1 "$@"
}
materialize
while test -f "$DATASET_DIR/_INCOMPLETE"; do materialize --resume; done
```

## 4. Validation

```bash
uv run --locked --project ai_platform/ml python -m ai_platform.ml.training.validate_materialized \
  --source "$SOURCE_DIR" \
  --dataset "$DATASET_DIR"
test ! -e "$DATASET_DIR/_INCOMPLETE"
sha256sum "$DATASET_DIR/manifest.json" | awk '{print $1}' > "$DATASET_DIR/manifest.sha256"
```

Validation must pass row parity, unique transaction IDs, chronological split
boundaries, labels, exact 50-column record schema, ordered 41-model-feature
schema, derived formulas, null merchant/cold-start preservation, and leakage
blacklists.

## 5. Immutable publication

The command rejects any existing or partial destination prefix. Every object is
created with GCS generation-match zero and read back by generation. The final
commit marker `_control/publication_manifest.json` is created last.

```bash
uv run --locked --no-sync --project ai_platform/features/feast python -m ops.gcp.publish_training_snapshot \
  --project-id "$PROJECT_ID" \
  --data-bucket "$DATA_BUCKET" \
  --dataset-version "$DATASET_VERSION" \
  --silver-run-id "$SILVER_RUN_ID" \
  --feature-run-id "$FEATURE_RUN_ID" \
  --snapshot-id "$TRAINING_SNAPSHOT_ID" \
  --feast-snapshot-id "$FEAST_SNAPSHOT_ID" \
  --source-git-sha "$SOURCE_GIT_SHA" \
  --feature-selection-digest "$FEATURE_SELECTION_DIGEST" \
  --source "$DATASET_DIR" \
  --pit-evidence "$PIT_EVIDENCE" \
  --trusted-registry-readback-evidence "$REGISTRY_READBACK_EVIDENCE" \
  --expected-registry-uri "gs://$DATA_BUCKET/features/registries/$DATASET_VERSION/$FEAST_SNAPSHOT_ID/registry.db" \
  --expected-registry-generation "$REGISTRY_GENERATION" \
  --expected-registry-sha256 "$REGISTRY_SHA256" \
  --expected-registry-execution-git-sha "$REGISTRY_EXECUTION_GIT_SHA" \
  --execution-git-sha "$ML1_EXECUTION_GIT_SHA" \
  --authorization CREATE_ONLY_TRAINING_SNAPSHOT \
  --evidence-output "$PUBLICATION_EVIDENCE"
test -s "$PUBLICATION_EVIDENCE"
```

Never delete or reuse a partial prefix after failure. Preserve it for
investigation and choose a new `TRAINING_SNAPSHOT_ID` only after review.

## 6. DVC versioning

Only the small provenance descriptor is tracked. The Training Snapshot remains
in immutable GCS and the 100 GB lakehouse is never copied into DVC.

```bash
uv run --locked --project ai_platform/ml python -m ops.gcp.prepare_training_snapshot_dvc \
  --publication-evidence "$PUBLICATION_EVIDENCE" \
  --descriptor-output "$DVC_DESCRIPTOR" \
  --execute
"$DVC_BIN" add "$DVC_DESCRIPTOR"
test -s "$DVC_DESCRIPTOR.dvc"
git add "$DVC_DESCRIPTOR.dvc" .gitignore
```

Commit the descriptor metadata only after reviewing its manifest checksum,
completion-marker generation/checksum, Registry generation/checksum, GCS URI,
source lineage, and execution Git SHA.

## 7. Mandatory F9 immutable-artifact gate

The currently checked-in F9 image and compiled YAML are stale and are **not
deployable** for this snapshot contract. After separate approval for Cloud
Build/Artifact Registry costs, mint an image from the exact reviewed commit,
resolve its immutable digest, and compile the KFP YAML with that digest:

Run this step from a separate clean checkout at
`EXPECTED_REVIEWED_GIT_SHA`, after the snapshot/DVC operator evidence has been
preserved. Do not run it from a worktree containing pending DVC metadata.

```bash
export F9_IMAGE_REPOSITORY="${REGION}-docker.pkg.dev/${PROJECT_ID}/ewallet-platform/f9-kfp-runtime"
export PIPELINE_PACKAGE=ai_platform/ml/pipelines/compiled/f9_canonical_pipeline.yaml
export F9_ARTIFACT_EVIDENCE="runtime/cloud-training/$TRAINING_SNAPSHOT_ID/f9-artifact-evidence.json"
ops/gcp/prepare_f9_immutable_artifacts.sh
```

The procedure fails unless the image evidence names
`EXPECTED_REVIEWED_GIT_SHA`, uses an `@sha256:` image reference, the compiled
YAML contains exactly that image, and its checksum matches the evidence. Do not
run F9 until this gate passes. Building, pushing, compiling and executing F9
remain separately authorized runtime operations; none is performed by ML-1
source validation.
