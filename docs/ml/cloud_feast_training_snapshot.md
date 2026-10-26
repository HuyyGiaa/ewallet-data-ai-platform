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

The contract resolves to:

```text
input Silver:  gs://<bucket>/lakehouse/runs/<silver_run_id>/silver
input DP3:     gs://<bucket>/lakehouse/runs/<feature_run_id>/gold
Feast data:    gs://<bucket>/features/offline/snapshots/<dataset>/<feature_run>/<snapshot>
Feast registry:gs://<bucket>/features/registries/<dataset>/<snapshot>/registry.db
training:      gs://<bucket>/ml/training-snapshots/<dataset>/<snapshot>
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

Run only after DP3 evidence is PASS. Substitute the real successful run IDs:

```bash
export EWALLET_DATA_BUCKET="ewallet-data-ai-platform-hg26-data"
export DATASET_VERSION="canonical-100gb-v1"
export SILVER_RUN_ID="canonical-silver-<actual-stamp>"
export FEATURE_RUN_ID="canonical-features-<actual-stamp>"
export SNAPSHOT_ID="canonical-training-v1-$(date -u +%Y%m%d-%H%M%S)"

python -m ops.gcp.prepare_feast_training_boundary \
  --bucket "$EWALLET_DATA_BUCKET" \
  --dataset-version "$DATASET_VERSION" \
  --silver-run-id "$SILVER_RUN_ID" \
  --feature-run-id "$FEATURE_RUN_ID" \
  --snapshot-id "$SNAPSHOT_ID"
```

The command is a dry-run and prints the exact execution sequence. Before that
sequence, install the pinned Feast environment requirements, authenticate ADC,
and verify the three output prefixes are absent. Run the printed source export
once. Run the printed materializer repeatedly with `--resume` while
`_INCOMPLETE` exists; each process handles one measured-safe 1,000-row chunk.
Run validation before publishing. The printed `gcloud storage ls` must report
no matching objects; any permission error is a blocker, not proof of absence.

## Evidence gate

Record the resolved contract JSON plus:

- DP3 PASS evidence and exact four-table inventory;
- export manifest row parity, Delta versions, UTC-microsecond type, and paths;
- PIT sample: ten cases, 310 comparable values, zero mismatch, labels not
  passed to Feast;
- source manifest: label integrity, sample modulus, selected split profile;
- training manifest and validator PASS: row/key parity, split isolation,
  chronology, derived formulas, forbidden-column checks;
- final GCS object listing, byte count, and copied `manifest.json`.

If an export or publish is partial, keep it as failed evidence and use a new
`snapshot_id`; never repair a published prefix in place.
