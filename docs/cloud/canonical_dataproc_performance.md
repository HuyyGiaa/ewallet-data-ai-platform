# Canonical full-scale Spark A/B runbook

This runbook compares the **same full canonical workload** (`canonical-100gb-v1`,
100,156,470,260 source bytes, 369,600,000 logical transactions) at one Git SHA.
The 1–4 GB runs remain smoke tests and are not primary performance evidence.
The transformation code, Standard tier, runtime 2.2, driver/executor shape, TTL
8h, initial/maximum executor counts, quota ceiling, schemas and validation rules
are identical. This is a **package-level Spark optimization comparison**: the
measured total difference belongs to the complete tuning package and cannot be
attributed individually to AQE, skew handling, shuffle partitioning or the
performance allocation profile without a separate ablation experiment.

## Profiles

| Setting | Baseline | Optimized (production/canonical) |
|---|---:|---:|
| AQE | false | true |
| Skew join handling | false | true |
| Shuffle partitions | 200 (Spark default) | 400 (measured project setting) |
| Dynamic allocation | enabled | enabled, `profile=performance` |
| Initial executors | 6 | 6 |
| Maximum executors | 7 | 7 |
| Maximum resource shape | 1 driver + 7 executors | 1 driver + 7 executors |

The baseline uses Managed Spark's supported dynamic allocation rather than an
invented static mode. It starts with the same six executors as optimized but
does not select the performance allocation profile. Both profiles therefore
start at 28 vCPU/about 2,800 GB disk and share the same ceiling: 32 vCPU and
about 3,200 GB disk, under 32 vCPU / 4,096 GB.
The optimized profile is the only profile allowed into canonical Feast/ML.
Baseline is benchmark-only.

`canonical` remains an alias for `optimized` in the low-level submitter for old
commands. New Airflow runs use only `spark_profile=baseline|optimized`.

## Immutable benchmark lineage

Create one identity and reuse it for both profiles:

```bash
export SPARK_BENCHMARK_ID="ab-$(date -u +%Y%m%d%H%M%S)"
export EWALLET_DATA_BUCKET=ewallet-data-ai-platform-hg26-data
./scripts/start_airflow_local.sh
```

Every wrapper creates a fresh attempt suffix. Execute mode refuses a dirty
working tree, so the packaged Python code is bound to the printed Git SHA. Run
IDs encode benchmark, profile,
stage and attempt, while submitters validate the binding before `gcloud` runs.
Baseline and optimized therefore cannot share an output prefix. Never reuse or
delete abandoned evidence `canonical-dp2-20261025-100451` or
`canonical-silver-20261025-100451`.

The wrappers also resolve the full 40-character Git SHA and bind it into DAG
configuration, batch labels and Spark application arguments. Evidence collection
rejects a batch whose ID, profile, benchmark, Git SHA, runtime 2.2, Standard tier
or profile properties do not match the declared comparison.

## Dry-run and execution order

Always inspect dry-run output first. It prints every relevant Spark property.
Use the exact Bronze/Silver/Gold IDs printed by the preceding wrapper.

```bash
# Baseline, dry-run first
./scripts/run_dp1_canonical.sh --profile baseline --benchmark-id "$SPARK_BENCHMARK_ID"
./scripts/run_dp2_canonical.sh <baseline-bronze-run> --profile baseline
./scripts/run_dp3_canonical.sh <baseline-silver-run> <baseline-gold-run> --profile baseline

# Baseline, only after review
./scripts/run_dp1_canonical.sh --profile baseline --benchmark-id "$SPARK_BENCHMARK_ID" --execute
./scripts/run_dp2_canonical.sh <baseline-bronze-run> --profile baseline --execute
./scripts/run_dp3_canonical.sh <baseline-silver-run> <baseline-gold-run> --profile baseline --execute

# Optimized against the same benchmark ID and Git SHA
./scripts/run_dp1_canonical.sh --profile optimized --benchmark-id "$SPARK_BENCHMARK_ID" \
  --predecessor-batch-id <baseline-feature-run>
./scripts/run_dp1_canonical.sh --profile optimized --benchmark-id "$SPARK_BENCHMARK_ID" \
  --predecessor-batch-id <baseline-feature-run> --execute
./scripts/run_dp2_canonical.sh <optimized-bronze-run> --profile optimized --execute
./scripts/run_dp3_canonical.sh <optimized-silver-run> <optimized-gold-run> --profile optimized --execute
```

Finish DP1→DP2→DP3 baseline before starting optimized. A retry must call the
wrapper again so it receives a new immutable attempt ID.

## Dataproc quota handoff guard

Managed Spark reaching a terminal state does not guarantee that Compute quota
is immediately reusable. In the abandoned benchmark `ab-20261025112811`, the
baseline Silver batch became `SUCCEEDED` at `2026-10-25T13:07:53.355271Z`.
Gold was created at `13:08:20.606935Z` and failed provisioning before Spark ran:
it requested 28 vCPU/2,800 GB while GCP reported 0 vCPU/896 GB available. A
later read-only check showed both quota usages back at zero. The exact release
latency was not captured by that run, so the incident supplies a lower-bound
failure observation, not a constant delay.

Every canonical submit now has an explicit Airflow wait task. It reads
`CPUS_ALL_REGIONS` from structured `gcloud compute project-info describe` JSON
and regional `DISKS_TOTAL_GB` from structured `gcloud compute regions describe`
JSON. Requirements come from the same Spark profile used by the submitter: one
driver plus six initial executors at the Standard default 4-vCPU/400-GB process
shape, or 28 vCPU/2,800 GB. Both baseline and optimized use this guard.

The guard polls after 15 seconds, backs off to at most 30 seconds, and times out
after 15 minutes. It submits nothing on timeout; Airflow stops before the
downstream submit task and prints both required and last observed quota. Fixed
sleep is intentionally avoided because release latency varies. Each actual wait
writes an attempt-scoped JSON file under
`docs/evidence/quota_handoffs/<benchmark-id>/<next-batch-id>-quota.json` with the
predecessor terminal time where known, every quota observation, first ready
time, wait duration and observed release latency. DP1 accepts
`--predecessor-batch-id` so optimized DP1 can identify the completed baseline
Features batch; readiness is still checked when no predecessor is supplied.

The protected handoffs are external/previous-profile→Bronze, Bronze→Silver,
Silver→Gold and Gold→Features. This covers immediate DP1→DP2, DP2→DP3 and
baseline DP3→optimized DP1 starts as well as the in-DP2 transition that exposed
the race. Dry-run renders the requirements, polling policy, evidence path and
predecessor without reading quota or triggering Airflow. The quota wait is
operational handoff overhead. Never include it in Spark A/B runtime; continue to
measure only Managed Spark batch start→terminal, DCU, shuffle, properties and
task-level metrics.

The failed Gold batch and all `ab-20261025112811` outputs remain troubleshooting
evidence. The wrappers reject that benchmark ID. Create a new benchmark ID and
new run IDs for the final comparison; do not delete or reuse the failed batch.

## Equivalence and evidence

For each stage, produce deterministic summary JSON with the exact table set,
schema SHA-256, row count, deterministic content fingerprint and required
invariants. Then compare:

```bash
VALIDATION_BATCH="abval-$(date -u +%y%m%d%H%M%S)"
SUMMARY_URI="gs://ewallet-data-ai-platform-hg26-data/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/<profile>/<stage>-summary.json"
python -m ops.gcp.submit_spark_output_summary \
  --batch-id "$VALIDATION_BATCH" \
  --stage <dp1-bronze|dp2-silver|dp2-gold|dp3-features> \
  --benchmark-id "$SPARK_BENCHMARK_ID" \
  --spark-profile <baseline-or-optimized> --output-run-id <run-id> \
  --dataset-version canonical-100gb-v1 --git-sha "$(git rev-parse HEAD)" \
  --summary-uri "$SUMMARY_URI"
# Inspect the rendered command, then repeat with --execute after approval.
```

The submitter packages the same pinned Delta dependencies and runs the scan on
Managed Spark, where the GCS connector is available. The data scan is read-only;
only the immutable summary JSON is created. The content fingerprint is an
order-independent aggregate of per-row `xxhash64` values plus exact row counts,
schema hashes and stage invariants. It is suitable for detecting semantic A/B
differences but is not a cryptographic, row-by-row proof.

DP3 output evidence checks exact grain, non-null point-in-time timestamps and
label exclusion. The `[T-window,T)` no-leakage rule is proven by the executable
historical-window tests at the same Git SHA; it is not falsely inferred from
non-null timestamps alone. Gold evidence checks primary-key grains, fact-to-
dimension referential integrity, fact/OBT row parity and label exclusion.

Then compare the two summaries:

```bash
python -m ops.gcp.validate_spark_output_equivalence \
  --baseline-summary baseline-summary.json \
  --optimized-summary optimized-summary.json \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/equivalence/dp1-bronze.json"
```

Repeat for `dp2-silver`, `dp2-gold`, and `dp3-features`. This is a semantic,
table-level comparison; it does not claim byte-identical Delta files because
Spark can emit different physical file layouts.

After each batch, collect read-only Managed Spark evidence:

```bash
python -m ops.gcp.collect_spark_benchmark_evidence \
  --project ewallet-data-ai-platform-hg26 --region asia-southeast1 \
  --batch-id <batch-id> --benchmark-id "$SPARK_BENCHMARK_ID" \
  --spark-profile <baseline-or-optimized> --stage <stage> \
  --dataset-version canonical-100gb-v1 --git-sha "$(git rev-parse HEAD)" \
  --upstream-run-id <upstream> --output-run-id <output> \
  --validation-json <stage-validation.json> \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/<profile>/<stage>.json"
```

The collector records state/times, runtime properties, DCU, shuffle usage and
driver URI; `milliDcuSeconds / 3,600,000` is DCU-hours. Once all eight batches
succeeded and four equivalence files pass:

```bash
python -m ops.gcp.compare_spark_benchmark \
  --evidence-root "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}"
```

The generator writes `comparison.json` and `comparison.csv`. It rejects mixed
datasets, Git SHAs, stages, unsuccessful batches or inequivalent outputs. A
negative percentage is explicitly reported as a regression. Only the optimized
Gold/features lineage may become the downstream Feast/ML source.
