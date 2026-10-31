# Bản đồ reuse legacy → cloud EDAI-1

| Capability | Reuse | Mở rộng cloud | Không copy |
|---|---|---|---|
| Source identity | `ShardPlan`, manifest digest, file hashes | Manifest-only profiler, truth boundary cho skew/cardinality | Regenerate 100 GB |
| Airflow | DP1/DP2/DP3 DAG IDs, task IDs, run-scoped paths | Read-only log evidence collector, SHA-256, failure/recovery chain | Tạo metadata/Airflow UI giả |
| DataHub | assertion evaluator, stable URN concepts | GCS physical URN, Airflow DataFlow/DataJob, idempotent publish+readback gate | Trino local URN như cloud truth |
| Storage | table/access-pattern inventory, equivalence rules | Isolated GCS Delta A/B harness cherry-picked từ `223257d` | Ghi đè/VACUUM canonical Delta |
| Warehouse | query/identifier safety idea | Isolated PostgreSQL fixture, 7 repeats, EXPLAIN ANALYZE BUFFERS, write trade-off | MLflow PostgreSQL |
| Docker | existing `.dockerignore`, digest discipline | catalog, comparable targets, multistage/nonroot, measured A/B | So sánh hai app khác chức năng |
| Schema | canonical table tuples/contracts | generated all-zone inventory + Mermaid | Tuyên bố SCD2/created đã deploy |

Module mới chỉ được tạo tại boundary thiếu rõ ràng: evidence recovery,
cloud operational lineage/readback, isolated warehouse benchmark, manifest truth
profile và schema inventory. Business rules/validators hiện có không bị viết lại.
