# Cloud Feast offline and bounded training snapshot

## Frozen milestone

This milestone starts only after canonical DP3 succeeds. It converts the four
immutable DP3 Delta feature tables into one immutable Feast Parquet snapshot,
validates point-in-time retrieval, and creates one bounded, versioned training
snapshot. Feast Online, Redis, model training, and serving remain out of scope.

## Input and output contract

Required identifiers are explicit and immutable:

- `dataset_version=canonical-100gb-v1`;
- `silver_run_id` is the successful canonical DP2 Silver run;
- `feature_run_id` is the successful canonical DP3 feature run;
- `snapshot_id` is new and must never be reused.

The feature run must also have one immutable
`feast-feature-selection-v1` evidence artifact with `PASS` status and a valid
digest. It binds the exact dataset, Silver run, DP3 run/root, Git SHA and Spark
profile. The selection may name either `baseline` or `optimized`; eligibility
comes from reviewed DP3 evidence, not from a permanent profile-name rule. The
current accepted optimized DP3 output can therefore be used now, while a later
verified Method 2 winner can become the source without changing this contract.

The contract resolves to:

```text
input Silver:  gs://<bucket>/lakehouse/runs/<silver_run_id>/silver
input DP3:     gs://<bucket>/lakehouse/runs/<feature_run_id>/gold
Feast data:    gs://<bucket>/features/offline/snapshots/<dataset>/<feature_run>/<snapshot>
Feast registry:gs://<bucket>/features/registries/<dataset>/<snapshot>/registry.db
training:      gs://<bucket>/ml/training-snapshots/<dataset>/<snapshot>
validation staging: gs://<bucket>/ml/training-staging/<dataset>/<snapshot>/validation
source staging:     gs://<bucket>/ml/training-staging/<dataset>/<snapshot>/source
```

The DP3 input must contain exactly `feat_user_behavior`,
`feat_account_behavior`, `feat_device_behavior`, and
`feat_merchant_behavior`. The exporter preserves `timestamp[us, tz=UTC]` and
refuses to replace an existing cloud table directory. The bounded training
source is derived from the matching canonical Silver `transactions` and
`fraud_labels`, selected with the existing label-independent hash rule using
`xxhash64(event_timestamp_us) % 10000 == 0`. At the canonical 369.6-million
logical-transaction scale this targets roughly 37,000 request rows, close to
the measured 40,148-row local F7 boundary; it does not attempt a 3.7-million
row local Feast retrieval.

## GCP execution boundary

The two jobs that scan canonical Silver or combine Silver with DP3 Gold run as
Managed Service for Apache Spark batches. They must not be invoked through the
Feast or F9 `uv` environments on a laptop:

- validation-sample export uses the explicit `dp3-features` Spark stage;
- bounded training-source export uses the explicit `dp2-silver` Spark stage;
- both pin Managed Spark runtime `2.2`, Standard tier, the existing checksum-
  pinned Delta 3.2.0 JAR set, the GCP runtime YAML, and the repository Python
  archive;
- runtime 2.2 supplies PySpark, pandas, PyArrow, Delta Python support, and the
  Cloud Storage connector. `spark.master` remains unset in repository config
  because the managed runtime owns it. A local Spark master, missing Delta
  configuration, or missing GCS filesystem fails closed at job startup;
- the Feast and F9 project locks remain dependency boundaries for their local
  retrieval/materialization and model workloads. They do not install the
  Dataproc runtime and therefore do not add PySpark or `delta-spark`.

Each Spark export writes to a new immutable GCS staging root. Its
`manifest.json` is written last and is the completion marker. Only the bounded
validation sample (10 canonical + 100 benchmark rows) and the bounded training
source (expected roughly 37,000 rows) are copied to local staging. The full
Silver population is never copied to the laptop.

## Temporal and leakage gates

- Historical windows remain `[T-window, T)`: the current transaction,
  same-timestamp peers, and future rows do not enter a feature snapshot.
- The Feast entity dataframe contains only entity keys and `event_timestamp`.
- `label` and `fraud_type` remain outside all four FeatureViews and enter only
  when the bounded Silver source is assembled.
- UTC microseconds are preserved; Trino's millisecond projection is not used.
- The existing chronological train/validation/test boundaries remain fixed.
- `status`, `new_balance`, `ingested_at`, labels, and entity IDs are excluded
  from model features by the existing F7 validation contract.

## Future USER_RUN

Run only after DP3 evidence is PASS. The current reviewed lineage is:

```bash
export EWALLET_DATA_BUCKET="ewallet-data-ai-platform-hg26-data"
export DATASET_VERSION="canonical-100gb-v1"
export SILVER_RUN_ID="ab-20261025134342-optimized-silver-r261025181923"
export FEATURE_RUN_ID="ab-20261025134342-optimized-feat-r261026025955"
export SNAPSHOT_ID="canonical-training-v1-20261028-101106"
export FEATURE_SELECTION_DIGEST="sha256:903f3a73a2843f373481479c0d271816621ed502bd254c642e6ee20fa12aee62"
export FEATURE_SELECTION_EVIDENCE="<absolute-path-to-reviewed-feast-feature-selection-v1.json>"

test "$(git rev-parse --abbrev-ref HEAD)" = "feature/phase2-fraud-platform"
test -z "$(git status --porcelain)"
python -m ops.gcp.prepare_feast_training_boundary \
  --bucket "$EWALLET_DATA_BUCKET" \
  --dataset-version "$DATASET_VERSION" \
  --silver-run-id "$SILVER_RUN_ID" \
  --feature-run-id "$FEATURE_RUN_ID" \
  --snapshot-id "$SNAPSHOT_ID" \
  --feature-selection-evidence "$FEATURE_SELECTION_EVIDENCE"
```

The command above is read-only and prints the exact ordered commands. Review
the resolved roots and digest, then run the printed commands one at a time. The
Spark portions have this shape:

```bash
export FEAST_VALIDATION_BATCH_ID="feast-val-$(date -u +%y%m%d%H%M%S)"
python -m ops.gcp.submit_feast_training_export \
  --job validation-samples \
  --batch-id "$FEAST_VALIDATION_BATCH_ID" \
  --dataset-version "$DATASET_VERSION" \
  --silver-run-id "$SILVER_RUN_ID" \
  --feature-run-id "$FEATURE_RUN_ID" \
  --snapshot-id "$SNAPSHOT_ID" \
  --feature-selection-evidence "$FEATURE_SELECTION_EVIDENCE" \
  --ttl 8h \
  --execute

gcloud dataproc batches describe "$FEAST_VALIDATION_BATCH_ID" \
  --region asia-southeast1 \
  --format='yaml(state,stateTime,createTime,runtimeInfo,approximateUsage)'

export TRAINING_SOURCE_BATCH_ID="train-src-$(date -u +%y%m%d%H%M%S)"
python -m ops.gcp.submit_feast_training_export \
  --job training-source \
  --batch-id "$TRAINING_SOURCE_BATCH_ID" \
  --dataset-version "$DATASET_VERSION" \
  --silver-run-id "$SILVER_RUN_ID" \
  --feature-run-id "$FEATURE_RUN_ID" \
  --snapshot-id "$SNAPSHOT_ID" \
  --feature-selection-evidence "$FEATURE_SELECTION_EVIDENCE" \
  --sample-modulus 10000 \
  --ttl 8h \
  --execute

gcloud dataproc batches describe "$TRAINING_SOURCE_BATCH_ID" \
  --region asia-southeast1 \
  --format='yaml(state,stateTime,createTime,runtimeInfo,approximateUsage)'
```

Both jobs use the existing quota-safe profile: one 4-vCPU driver, six initial
4-vCPU executors and seven maximum executors. Initial allocation is 28 vCPU and
about 2,800 GiB batch disk; the ceiling is 32 vCPU and about 3,200 GiB. `8h` is
a cleanup TTL ceiling, not an expected runtime. Use `stateTime` and
`approximateUsage` from the describe command for actual duration and DCU
evidence.

After each batch succeeds, copy only its completed immutable output and verify
the completion manifest before retrieval/materialization:

```bash
export LOCAL_ROOT="runtime/cloud-training/$SNAPSHOT_ID"
test ! -e "$LOCAL_ROOT/validation"
gcloud storage rsync --recursive \
  "gs://$EWALLET_DATA_BUCKET/ml/training-staging/$DATASET_VERSION/$SNAPSHOT_ID/validation" \
  "$LOCAL_ROOT/validation"

uv run --project ai_platform/features/feast python -m \
  ai_platform.features.feast.validation.canonical_sample \
  --environment gcp \
  --sample-path "$LOCAL_ROOT/validation/canonical_sample"

test ! -e "$LOCAL_ROOT/source"
gcloud storage rsync --recursive \
  "gs://$EWALLET_DATA_BUCKET/ml/training-staging/$DATASET_VERSION/$SNAPSHOT_ID/source" \
  "$LOCAL_ROOT/source"

uv run --locked --project ai_platform/features/feast python -m ai_platform.ml.training.materialize \
  --environment gcp \
  --dataset-version "$DATASET_VERSION" \
  --source-silver-run-id "$SILVER_RUN_ID" \
  --source-feature-run-id "$FEATURE_RUN_ID" \
  --snapshot-id "$SNAPSHOT_ID" \
  --source-git-sha bbae48caef7f65fda0c9375a7a49afb2eca5eef4 \
  --feature-selection-digest "$FEATURE_SELECTION_DIGEST" \
  --source "$LOCAL_ROOT/source" \
  --output "$LOCAL_ROOT/dataset" \
  --chunk-size 1000 \
  --max-new-chunks 1

while test -f "$LOCAL_ROOT/dataset/_INCOMPLETE"; do
  uv run --locked --project ai_platform/features/feast python -m ai_platform.ml.training.materialize \
    --environment gcp \
    --dataset-version "$DATASET_VERSION" \
    --source-silver-run-id "$SILVER_RUN_ID" \
    --source-feature-run-id "$FEATURE_RUN_ID" \
    --snapshot-id "$SNAPSHOT_ID" \
    --source-git-sha bbae48caef7f65fda0c9375a7a49afb2eca5eef4 \
    --feature-selection-digest "$FEATURE_SELECTION_DIGEST" \
    --source "$LOCAL_ROOT/source" \
    --output "$LOCAL_ROOT/dataset" \
    --chunk-size 1000 \
    --max-new-chunks 1 \
    --resume
done

uv run --project ai_platform/ml python -m \
  ai_platform.ml.training.validate_materialized \
  --source "$LOCAL_ROOT/source" \
  --dataset "$LOCAL_ROOT/dataset"
```

Do not rerun a failed export into a partial prefix. Preserve its batch and GCS
prefix as evidence, choose a new `snapshot_id`, and render the sequence again.
The materializer is the only resumable step: `_INCOMPLETE` records progress,
and each `--resume` process handles at most one 1,000-row chunk. Run final
validation before publishing. The printed `gcloud storage ls` must report no
matching objects; any permission error is a blocker, not proof of absence.

## Evidence gate

Record the resolved contract JSON plus:

- DP3 PASS evidence and exact four-table inventory;
- export manifest row parity, Delta versions, UTC-microsecond type, and paths;
- PIT sample: ten cases, 310 comparable values, zero mismatch, labels not
  passed to Feast;
- source manifest: label integrity, sample modulus, selected split profile;
- training manifest and validator PASS: row/key parity, split isolation,
  chronology, derived formulas, forbidden-column checks;
- final GCS object listing, byte count, copied `manifest.json`, and the exact
  `manifest.sha256` uploaded beside it. F9 and KFP must receive this expected
  digest and reject a downloaded manifest that does not match.

If an export or publish is partial, keep it as failed evidence and use a new
`snapshot_id`; never repair a published prefix in place.
