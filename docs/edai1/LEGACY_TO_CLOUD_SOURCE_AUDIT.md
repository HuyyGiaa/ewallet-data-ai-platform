# Audit source `data_platform/` trước triển khai EDAI-1

Mốc: branch `codex/edai1-implementation`, base
`0ff916dc840edebc3559ee3bf317f239e0fa1a51`. Inventory gồm 119 tệp không kể
cache/log/runtime; danh sách `rg --files data_platform | sort` có SHA-256
`36a87fd3f33c812ba08694cd272e31018e6b86980823e7fe97328169b3e3f504`.

| Vùng | Số tệp | Kết luận |
|---|---:|---|
| config | 6 | Runtime profile và Spark profile hiện hành; không sửa Spark. |
| contracts | 11 | Contract YAML là nguồn chuẩn cho schema/quality inventory. |
| generation | 20 | Deterministic sharding/manifest có thể tái dùng; offline pandas generator chỉ là logic tham khảo cục bộ. |
| ingestion | 6 | DP1 cloud thật nằm ở Spark/ops hiện hành; Debezium/Kafka compose là local legacy. |
| metadata | 9 | Lineage/assertion Trino cũ tái dùng rule/identity idea; URN Trino local không dùng cho GCS cloud. |
| orchestration | 7 | Ba DAG DP1/DP2/DP3 là call path hiện hành; không submit lại 100 GB. |
| processing | 37 | Spark owner; chỉ đọc để xác định table/schema, không sửa. |
| quality | 4 | Rule chất lượng và validator log hiện hành có thể liên kết bằng chứng. |
| storage | 18 | Table inventory/URI validation tái dùng; MinIO/Trino mutation là local-only. |

## Call path thực

- DP1: Airflow `dp1_source_to_bronze` → `ops.gcp.submit_source_to_bronze` →
  immutable GCS manifest → run-scoped Bronze → validator.
- DP2: Airflow `dp2_bronze_to_curated` → Silver → core Gold → validators.
- DP3: Airflow `dp3_historical_features` → four run-scoped feature tables → validator.
- DataHub legacy: Trino dataset lineage + contract assertions, không có
  `DataFlow/DataJob`; snapshot lịch sử không phải production cloud.
- Storage legacy: MinIO Delta inspection/compaction và Trino query benchmark;
  bằng chứng 500K giữ nguyên nhãn `LOCAL_500K`.

## Ranh giới không tái dùng trực tiếp

Không copy credentials/endpoints MinIO, local Compose paths, Trino URN hoặc
full-refresh writer sang cloud. Không sửa Spark/Flink, Feast/MLOps hay Block B.
SCD2 và cột `created` chỉ có proposal bàn giao owner; PIT 310-value do MLOps owner.
