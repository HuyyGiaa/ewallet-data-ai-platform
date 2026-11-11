# Evidence: 5GB rehearsal benchmark `ab-20261110225456`

This folder holds the evidence for the [5GB Rehearsal Benchmark report](../../../../edai1/spark/REHEARSAL_5GB_BENCHMARK.md).

- **Benchmark:** `rehearsal-5gb-v1`, pinned to `aae22b08eee20fa055c4aefc8d23c14cc3ae5f8c`.
- **Source manifest SHA-256:** `ac2dfb0e…7123eb80`.
- **Collected:** 2026-11-11, read-only. No cloud object was written or changed.
- **Not yet here:** the cross-profile semantic comparison. Batch `c1-20261110225456` was `RUNNING` at 10:30Z and no `comparison.json` existed, so semantic equivalence is PENDING / NOT VERIFIED.

| Path | Content | Produced by |
|---|---|---|
| `manifests/<profile>/` | Byte copies of `dp1_handoff.json`, `dp2_handoff.json`, `dp3_completion.json` and `campaign_manifest.json` from `gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/lakehouse/campaigns/ab-20261110225456/<profile>/` | `gcloud storage cp` |
| `stage_reports/<profile>-<stage>-<kind>.json` | One report per Spark application. `kind` is `transform` or `validate` | `python -m ops.gcp.spark_stage_report report --event-log <app>.zstd` |
| `stage_comparisons/<stage>-<kind>.json` | Baseline against Optimised for one application pair | `python -m ops.gcp.spark_stage_report compare --input-identity ab-20261110225456/rehearsal-5gb-v1/ac2dfb0e` |
| `derived_metrics.json` | AQE plan facts, Spark jobs, executor run and GC time, large shuffle stages with fewer tasks than the 24 initial cores, first-executor latency | `tools/derive_metrics.py` |
| `dataproc_usage.json` | Batch state and times, runtime version, approximate DCU and shuffle-storage usage, applied AQE, shuffle and allocation properties. Service account and network fields are left out | `tools/derive_metrics.py` over `gcloud dataproc batches describe --format=json` |
| `airflow_dagruns.json` | DagRun and task-instance states and durations | `tools/derive_metrics.py`, which opens the Airflow SQLite metadata read-only |
| `validation_reports/<profile>-<stage>.json` | Byte copies of the 8 `validation-<scope>-a1.json` reports named in the manifests: rule name, expected and observed value, PASS/FAIL | `gcloud storage cp` |
| `screenshots/` | 21 genuine owner captures (Airflow, Spark UI, validation logs), copied unchanged from `~/Pictures/Screenshots/`; purpose, verified reference and SHA-256 per image in [`screenshots/MANIFEST.md`](screenshots/MANIFEST.md) (`MANIFEST.json` is the same data) | `cp -p`; SHA-256 equal to the originals |
| `EVENT_LOGS.sha256` | SHA-256 of the 16 Spark event logs, which stay in GCS | `sha256sum` of the downloaded copies |
| `SHA256SUMS` | Checksums of every file in this folder | `sha256sum` |

Regenerate the derived files from fresh read-only downloads:

```bash
python -m ops.gcp.spark_stage_report report --event-log <dir>/<application id>.zstd --output stage_reports/<name>.json
python docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/tools/derive_metrics.py \
  --event-logs <dir with the 16 .zstd logs> --batches <dir with the 16 describe JSONs> \
  --manifests docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/manifests \
  --airflow-db data_platform/orchestration/airflow/airflow.db --output-dir <out>
```

Batch IDs, Spark application IDs and event-log URIs are listed in each `campaign_manifest.json` under `stages.<stage>.execution_metadata` and `stages.<stage>.validation.execution_metadata`.

**Screenshots.** Only real captures are included. None was generated, edited or cropped.
