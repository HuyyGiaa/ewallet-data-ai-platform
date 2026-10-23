# Canonical 100 GB cloud data readiness

The canonical source gate and DP1 Source-to-Bronze run have passed. The
authoritative source dataset is `canonical-100gb-v1`; the successful DP1
lineage binds Bronze run `canonical-bronze-20261025-084645`. The remaining
user-operated canonical path starts at DP2.

## Measured source plan

`configs/generation/cloud-canonical-100gb.yaml` was calibrated with one
deterministic 100,000-transaction shard. The shard produced 27,059,611 bytes
in 20.585 seconds with 300,478,464 bytes peak RSS. The resulting plan is 3,696
shards, 369,600,000 logical transactions, and an estimated 100,012,322,256
Parquet bytes. Local disk safety passed with more than 141 GiB remaining after
the staging allowance.

The completed GCS manifest is authoritative and passed the final gate at
100,156,470,260 bytes.

## Bounded local parallel-generation benchmark

The shard generator was benchmarked with four canonical-size shards, each
containing 12,500 users, 300 merchants, and 100,000 logical transactions.
All modes used `global_seed=7001` and the same immutable shard plan.

| Workers | Wall time | Speedup | Peak aggregate RSS |
| ---: | ---: | ---: | ---: |
| 1 | 98.791 s | 1.00x | not recorded |
| 2 | 47.094 s | 2.10x | not recorded |
| 4 | 28.212 s | 3.50x | 1.314 GB |

This is a generation-only benchmark. It excludes GCS upload, remote checksum
verification, and final manifest publication. It does not establish or claim
the runtime of the canonical 100 GB run.

The sequential, two-worker, and four-worker runs produced byte-identical
Parquet files and identical per-shard manifests and checksums. Shard seeds are
derived only from the immutable plan and shard ID. Each `ProcessPoolExecutor`
worker owns one unique shard staging directory, so process-local random state
cannot leak between shards. The main process alone validates `plan.json`,
selects unfinished shard IDs, uploads and verifies outputs, writes completion
manifests, and calls `finalize()`. In-flight futures are limited to the worker
count. A failed worker stops the run while already published shards remain
verified and resumable. Four workers are therefore the measured local setting
for the canonical USER_RUN command.

## USER_RUN: generate and publish the immutable source

Run from the repository root in the `fintech` Conda environment:

```bash
export GCP_PROJECT_ID=ewallet-data-ai-platform-hg26
export GCP_REGION=asia-southeast1
export EWALLET_DATA_BUCKET=ewallet-data-ai-platform-hg26-data
export DATAHUB_GMS_URL=http://127.0.0.1:8080
export DATASET_VERSION=canonical-100gb-v1
export CALIBRATION_REPORT=tmp/source-calibration/cloud-canonical-100gb.json

python -m ops.gcp.calibrate_source_scale \
  --profile configs/generation/cloud-canonical-100gb.yaml \
  --config data_platform/generation/config/settings.yaml \
  --report "$CALIBRATION_REPORT"

export SHARD_COUNT="$(python -c 'import json,os; print(json.load(open(os.environ["CALIBRATION_REPORT"]))["derived_plan"]["shard_count"])')"
test "$SHARD_COUNT" = "3696"

python -m ops.gcp.generate_source_dataset \
  --environment gcp \
  --config data_platform/generation/config/settings.yaml \
  --dataset-version "$DATASET_VERSION" \
  --global-seed 7001 \
  --shard-count "$SHARD_COUNT" \
  --users-per-shard 12500 \
  --merchants-per-shard 300 \
  --transactions-per-shard 100000 \
  --workers 4

python -m ops.gcp.generate_source_dataset \
  --environment gcp \
  --config data_platform/generation/config/settings.yaml \
  --dataset-version "$DATASET_VERSION" \
  --global-seed 7001 \
  --shard-count "$SHARD_COUNT" \
  --users-per-shard 12500 \
  --merchants-per-shard 300 \
  --transactions-per-shard 100000 \
  --workers 4 \
  --execute
```

The execute command is resumable and verifies every uploaded object. Keep the
same plan and dataset version when resuming.

## USER_RUN: validate the canonical source final gate

Wait for the generator process to exit successfully, then run the optimized
gate before DP1:

```bash
export GCP_PROJECT_ID=ewallet-data-ai-platform-hg26
export EWALLET_DATA_BUCKET=ewallet-data-ai-platform-hg26-data
export DATASET_VERSION=canonical-100gb-v1

python -m ops.gcp.validate_canonical_source_gate \
  --project "$GCP_PROJECT_ID" \
  --bucket "$EWALLET_DATA_BUCKET" \
  --dataset-version "$DATASET_VERSION"
```

The gate requires a `COMPLETE` dataset manifest, exactly 3,696 shard
manifests, 369,600,000 logical transactions, 376,992,000 physical
transactions, 369,600,000 fraud-label rows, 7,392,000 fraud rows, exactly
33,264 Parquet objects, and 100-105 GB of manifest-backed Parquet data.

Object verification uses one paginated `list_blobs()` scan for the dataset
prefix. It builds an in-memory map for Parquet metadata only; `_control` JSON
objects are excluded from the 33,264-object count. Every expected path must be
present, every size must match, and every listed MD5 must match its manifest
value. A per-object metadata reload is used only when that object's listing
entry does not expose an MD5. The gate fails if the fallback still cannot
obtain an MD5.

## USER_RUN: orchestrate Bronze, Silver, Gold, and features

After a reboot, start or reuse the three required Airflow 3 components with one
command. The helper uses the dedicated `airflow` conda environment, the repo's
existing metadata database, port 8084, and the configured execution API. It
never initializes or resets metadata state:

```bash
./scripts/start_airflow_local.sh
```

DP1 explicitly selects the canonical performance profile. See
[Canonical Dataproc performance profile](canonical_dataproc_performance.md)
for its command and exact Spark properties.

After DP1 passes, first render DP2 without triggering anything, then explicitly
execute it with the exact successful canonical Bronze run ID:

```bash
./scripts/run_dp2_canonical.sh canonical-bronze-<successful-stamp>
./scripts/run_dp2_canonical.sh canonical-bronze-<successful-stamp> --execute
```

The wrapper creates fresh Silver, Gold, and DP2 IDs, binds the supplied Bronze
run, and explicitly sends `dataproc_performance_profile=canonical` and
`dataproc_ttl=8h`. The DAG's first task verifies the exact eight-table Bronze
input and Delta logs before any Silver submission.

After DP2 passes, use the exact successful Silver and core Gold IDs printed by
that invocation:

```bash
./scripts/run_dp3_canonical.sh canonical-silver-<successful-stamp> canonical-gold-<successful-stamp>
./scripts/run_dp3_canonical.sh canonical-silver-<successful-stamp> canonical-gold-<successful-stamp> --execute
```

The DP3 DAG verifies the exact Silver and core Gold table sets and Delta logs
before submitting features. Both wrappers reject pilot/stale ID formats, print
all resolved IDs and the exact Airflow 3 command, and default to dry-run.

Never reuse a failed or successful Managed Spark batch ID. If DP2 may have
touched Silver, rerun the wrapper to create a new DP2, Silver, and Gold ID. If
Silver passed but Gold failed, keep that Silver output as evidence and still
use a fresh DP2/Silver/Gold set because the DP2 DAG is an end-to-end unit. A
DP3 retry keeps the same verified Silver/Gold inputs but must use a fresh DP3
and feature ID. Existing outputs are immutable; each submitter refuses a
non-empty output prefix before submission.

Machine-readable evidence: [100 GB readiness evidence](../evidence/cloud/s1b_100gb_readiness.json).
