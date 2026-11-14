# Feature Store screenshots

The owner captured all of these screenshots by hand from the running systems. They
were copied unchanged; `SHA256SUMS` lists the digest of each file. They are
explained in [FEATURE_STORE.md](../../../../edai2/mlops/FEATURE_STORE.md).

| File | Shows | Run | Supporting raw evidence |
|---|---|---|---|
| `batch-01_feast_offline_snapshot_gcs.png` | The snapshot folder in GCS: `_control/` and the four `feat_*` tables | snapshot `feast-r5gb-20261111-214544` | [`export_manifest.json`](../fs-20261112/phase-a-export/export_manifest.json) |
| `batch-02_feast_snapshot_manifest_validation.png` | Source rows equal output rows for every table, 63,238,144 in total, and the manifest SHA-256 | same | same, [`independent_verification.txt`](../fs-20261112/phase-a-export/independent_verification.txt) |
| `batch-03_feast_historical_retrieval.png` | `get_historical_features` on the snapshot: 20 rows, 31 features, 155 comparisons, 0 mismatches | same | [`historical_retrieval.json`](../fs-20261112/phase-a-export/historical_retrieval.json) |
| `batch-04_airflow_incremental_materialization_success.png` | Airflow 3 graph and task list: eight tasks, all successful | DagRun `manual__fs11-20261112T084109Z` | [`row11/manual__fs11-20261112T084109Z/`](../fs-cloud-20261112/row11/manual__fs11-20261112T084109Z/) |
| `batch-05_datahub_materialization_lineage.png` | DataHub lineage: four snapshot datasets in GCS → `materialize_window` → four Feast Online datasets | same | [`08-datahub-lineage.json`](../fs-cloud-20261112/row11/manual__fs11-20261112T084109Z/08-datahub-lineage.json) |
| `batch-06_materialization_window_and_online_readback.png` | `NEW_WINDOW` 18:00 → 23:00 UTC, registry generation change, readback 155/0 | same | `03-window.json`, `04-materialize.json`, `06-readback.json` |
| `batch-07_materialization_source_and_image_provenance.png` | DAG run, source commit, digest-pinned operator image, snapshot ID | same | `07-provenance.json` |
| `stream-01_flink_running_jobs.png` | Flink 1.19.1 dashboard: one running job, 4/4 tasks, one TaskManager | job `3dca1e262210328f14d0585f3f8ea70e`, campaign `verify-fs12-20261114063323` | [`flink-jobs.json`](../fs12-cloud-20261114/row12/verify-fs12-20261114063323/flink-jobs.json) |
| `stream-02_flink_job_graph.png` | Job graph: source → dedup → event-time disposition → feature state, with record counts | same | same |
| `stream-03_flink_checkpoints.png` | Checkpoints: 13 triggered, 13 completed, 0 failed | same | — (dashboard only) |
| `stream-04_gcs_offline_parquet_files.png` | Offline Parquet files per view in GCS: 14 + 14 + 5 + 14 = 47 | same | [`block-a-offline.log`](../fs12-cloud-20261114/row12/verify-fs12-20261114063323/block-a-offline.log) |
| `stream-05_offline_row_kafka_offset_provenance.png` | One offset manifest: Kafka topic, partition and offset linked to its Parquet row and SHA-256 | campaign `verify-fs12-20261112084123` (earlier run, same code path) | [`gcs-campaign-other.txt`](../fs-cloud-20261112/row12/verify-fs12-20261112084123/gcs-campaign-other.txt) |
| `stream-06_redis_online_feature_keys.png` | Redis DB 9: Feast entity keys and the campaign owner marker | campaign `verify-fs12-20261114063323` | [`verify-fs12-20261114063323.preflight`](../fs12-cloud-20261114/row12/preflight/verify-fs12-20261114063323.preflight) |
| `stream-07_offline_online_consistency_pass.png` | Consistency report: 203 comparisons, 0 mismatches, 0 missing, 26 entities, PIT 26/26, digest | same | [`consistency-report.json`](../fs12-cloud-20261114/row12/verify-fs12-20261114063323/consistency-report.json) |
| `stream-08_kubernetes_jobs_completed.png` | The six streaming Jobs `Complete 1/1`, all with the same digest-pinned image | same | [`validator-job.json`](../fs12-cloud-20261114/row12/verify-fs12-20261114063323/validator-job.json) |

## Originals

The originals are in the owner's `~/Pictures/Screenshots/`. Only the file name
was changed when copying:

| Original | Copy |
|---|---|
| `FS11-00B_feast_offline_snapshot_gcs.png` | `batch-01_…` |
| `FS11-00C_feast_snapshot_manifest_validation.png` | `batch-02_…` |
| `FS11-00D_feast_historical_retrieval.png` | `batch-03_…` |
| `FS11-01_airflow_incremental_success.png` | `batch-04_…` |
| `FS11-02_datahub_materialization_lineage.png` | `batch-05_…` |
| `FS11-03_incremental_materialization_readback.png` | `batch-06_…` |
| `FS11-04_source_image_provenance.png` | `batch-07_…` |
| `FS12-01A_flink_running_jobs.png` | `stream-01_…` |
| `FS12-01B_flink_job_graph.png` | `stream-02_…` |
| `FS12-01C_flink_checkpoints.png` | `stream-03_…` |
| `FS12-02A_gcs_offline_parquet_files.png` | `stream-04_…` |
| `FS12-02_kafka_offset_provenance.png` | `stream-05_…` |
| `FS12-03A_redis_online_feature_keys.png` | `stream-06_…` |
| `FS12-04_offline_online_consistency_pass.png` | `stream-07_…` |
| `FS12-05_kubernetes_jobs_completed.png` | `stream-08_…` |

Two captures were left out:

- `FS12-04_streaming_consistency_pass.png` shows the consistency report of the
  2026-11-12 campaign. `stream-07` shows the same check for the 2026-11-14
  campaign.
- `FS12-05_source_image_provenance.png` shows the pod image IDs of the
  2026-11-12 campaign. `stream-08` shows the digests of the 2026-11-14 Jobs.

The DataHub view (`batch-05`) was captured after the local DataHub search index
was rebuilt. The lineage records themselves were written by the DagRun's
`publish_datahub_lineage` task. During that run OpenSearch was read-only
because its disk was full, so the records reached MySQL but were not
indexed. The rebuild restored the index for the `materialize_window` DataJob
and its datasets from those MySQL records. It also added a `status` aspect
(`removed: false`) to the four snapshot datasets: the job had referenced them
without creating them, so the lineage view hid them. Neither step changed the
DataJob's lineage edges or properties.
