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
| [Runtime configuration](cloud/configuration.md) | Local/dev/GCP profiles, environment variables and preflight | Cloud refactor operations |
| [Terraform GCP foundation](cloud/terraform_gcp_foundation.md) | Remote state, GCS/IAM/VPC/registry modules, cost gates and apply/cleanup runbook | R2 infrastructure operations |
| [Dataproc Delta-on-GCS smoke](cloud/dataproc_delta_smoke.md) | Cost-gated Spark/Delta/GCS compatibility job, submission, evidence and cleanup | R3 runtime decision checkpoint |
| [Cloud Silver bounded pilot](cloud/silver_bounded_pilot.md) | Dry-run and user execution contract for the verified R4 Bronze pilot | Stage 1A runbook |
| [Cloud core Gold bounded pilot](cloud/core_gold_bounded_pilot.md) | Immutable bounded Silver-to-core-Gold run and validation | Stage 1B verified evidence |
| [Cloud historical features bounded pilot](cloud/historical_features_bounded_pilot.md) | Immutable DP3 point-in-time feature run and validation | Stage 1B verified evidence |
| [Cloud Feast and bounded training snapshot](ml/cloud_feast_training_snapshot.md) | Frozen DP3-to-Feast offline contract and future USER_RUN boundary | Ready for canonical DP3 output |
| [Canonical F9 and MLflow](ml/f9_mlflow_canonical.md) | Bounded four-model experiment, PostgreSQL/GCS tracking, registry, evidence, and rollback | Ready for passed Feast snapshot |
| [Canonical 100 GB readiness](cloud/canonical_100gb_readiness.md) | Measured source plan and exact user-operated canonical commands | Ready, not executed |
| [R1 configuration evidence](evidence/cloud/r1_config_foundation.json) | Machine-readable profile, literal-scan, compile, and regression results | Verified foundation evidence |
| [R2 Terraform foundation evidence](evidence/cloud/r2_terraform_plan.json) | Applied state/bootstrap foundation, recovery record, final state counts and guardrails | Verified infrastructure evidence |
| [R3 Dataproc Delta evidence](evidence/cloud/r3_dataproc_delta_smoke.json) | Delta write/append/read-back, schema and timestamp precision on GCS | Verified cloud runtime evidence |
| [Silver bounded pilot evidence](evidence/cloud/s1b_silver_bounded_pilot.json) | Immutable Bronze-to-Silver run, persisted counts, schemas and DQ results | Stage 1B verified evidence |
| [Core Gold bounded pilot evidence](evidence/cloud/s1b_core_gold_bounded_pilot.json) | Immutable core Gold run, persisted counts and leakage checks | Stage 1B verified evidence |
| [Historical features bounded pilot evidence](evidence/cloud/s1b_historical_features_bounded_pilot.json) | Immutable DP3 run, feature counts and temporal checks | Stage 1B verified evidence |
| [Bounded Airflow verification evidence](evidence/cloud/s1b_airflow_bounded_verification.json) | DP1/DP2/DP3 DagRun replay and persisted Delta validations | Stage 1B verified evidence |
| [Fraud feature design](ml/fraud_feature_design.md) | Point-in-time user, account, device, and merchant feature contract | Implemented Gold contract |
| [Feast offline retrieval](ml/feast_offline.md) | Entities, FeatureViews, source adapter, precision, PIT retrieval, and limits | Implemented bounded offline retrieval |
| [Fraud training dataset](ml/fraud_training_dataset.md) | Supervised row contract, leakage boundary, temporal splits, and reproducible build | Implemented 40,148-row trainable materialization |
| [Random Forest fraud baseline](ml/fraud_modeling.md) | Active preprocessing/model contract, historical model-selection evidence, runtime artifacts, and validation/test results | Implemented F8 local baseline |

Evidence under `docs/evidence/` includes both historical run artifacts and
machine-readable refactor gates. Current operational commands and the
fresh-clone path are in the repository [README](../README.md).
