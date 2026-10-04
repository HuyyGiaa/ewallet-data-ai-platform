# Documentation index

Start with the [system architecture](00_system_architecture.md), then use the
component documents for implementation detail and recorded evidence.

| Document | Purpose | Current role |
|---|---|---|
| [System architecture](00_system_architecture.md) | Main architecture, data/control flow, status, ports, limitations | Canonical overview |
| [Data generator](01_data_generator.md) | Offline and streaming synthetic data behavior | Detailed reference |
| [Schema design](02_schema_design_example.md) | Schemas, grains, and Gold model | Detailed reference |
| [Batch pipeline](03_batch_pipeline.md) | Bronze, Silver, Gold transformations and checks | Detailed reference |
| [Streaming pipeline](04_streaming_pipeline.md) | Redpanda and PyFlink experiment | Experimental path |
| [Storage optimization](05_storage_optimization.md) | Delta layout and compaction benchmark | Recorded benchmark |
| [Airflow orchestration](06_airflow_orchestration.md) | DAG, retries, concurrency, quality gates | Operations reference |
| [Performance benchmarks](07_performance_benchmarks.md) | Spark skew and cardinality experiments | Recorded benchmark |
| [DataHub metadata and lineage](08_datahub_lineage.md) | Trino ingestion, direct lineage, assertions | Metadata operations |
| [Fraud feature design](ml/fraud_feature_design.md) | Point-in-time user, account, device, and merchant feature specification | Final design for implementation |

The PNG files under `docs/evidence/` are historical run evidence. Current
operational commands and the fresh-clone path are in the repository
[README](../README.md).
