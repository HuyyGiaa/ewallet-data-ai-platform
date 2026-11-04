# Legacy reuse verification

The complete local `data_platform/` tree was treated as a catalogue of proven ideas, not as a deployment template. The V5 implementation reuses contracts and algorithms only where the cloud architecture agrees.

| Area | Reused | Cloud adaptation | Rejected / reason |
|---|---|---|---|
| Storage | deterministic manifest, Delta compaction metrics, semantic hash checks | GCS URNs, scratch-only output and explicit TTL | MinIO paths and full 100 GB rewrite |
| Airflow | DP1/DP2/DP3 DAG topology and immutable log collector | historical run IDs and Dataproc batch IDs retained | rerunning historical DAGs; reconstructed task states |
| DataHub | aspect-level publish/readback pattern | Airflow flow/job URNs plus physical GCS dataset URNs | local Trino/MinIO URNs as cloud truth |
| Docker | multi-stage build, digest/size/smoke comparison | one-shot Compose profile, UID 65532, read-only/no-network runtime | stale Compose labels as evidence |

Every reused component is covered by a source test or an immutable runtime artifact. No Spark, Flink/Block A, Feast/MLOps or Block B source was changed.

