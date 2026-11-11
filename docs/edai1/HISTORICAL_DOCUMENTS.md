# EDAI-1 Historical Documents

The files below are internal working documents produced during development: handoffs, audits, reconciliations, proposals and earlier status reports. They are kept unchanged for traceability, but they are **not** the submission reports. Several of them describe states that were later superseded. The current technical reports are listed in the [EDAI-1 index](README.md).

These files were moved with `git mv` to `archive/legacy-docs/`; their content
and Git history remain available. The complete old-to-new path map is in the
[relocation manifest](../../archive/legacy-docs/README.md).

| File | Kind | Superseded by |
|---|---|---|
| [EDAI1_HANDOFF.md](../../archive/legacy-docs/docs/edai1/EDAI1_HANDOFF.md) | Development handoff | [README](README.md) |
| [spark/OPTIMIZATION_MATRIX.md](../../archive/legacy-docs/docs/edai1/spark/OPTIMIZATION_MATRIX.md) | Historical 100GB Spark experiments | [Batch Processing and Orchestration](BATCH_PROCESSING_AND_ORCHESTRATION.md) |
| [spark/REHEARSAL_5GB_BENCHMARK.md](../../archive/legacy-docs/docs/edai1/spark/REHEARSAL_5GB_BENCHMARK.md) | First 5GB rehearsal write-up | [Batch Processing and Orchestration](BATCH_PROCESSING_AND_ORCHESTRATION.md) |
| [airflow/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md](../../archive/legacy-docs/docs/edai1/airflow/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md) | Historical DAG versions and 100GB runs | [Batch Processing and Orchestration](BATCH_PROCESSING_AND_ORCHESTRATION.md) |
| [EDAI1_RUBRIC_100_STATUS.md](../../archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_100_STATUS.md) | Earlier rubric status | [RUBRIC_EVIDENCE_INDEX](RUBRIC_EVIDENCE_INDEX.md) |
| [EDAI1_RUBRIC_COVERAGE.md](../../archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_COVERAGE.md) | Earlier rubric coverage matrix | [RUBRIC_EVIDENCE_INDEX](RUBRIC_EVIDENCE_INDEX.md) |
| [RUBRIC_100_CROSS_WORKSTREAM.md](../../archive/legacy-docs/docs/edai1/RUBRIC_100_CROSS_WORKSTREAM.md) | Workstream boundary note | — |
| [RUNTIME_BLOCKERS.md](../../archive/legacy-docs/docs/edai1/RUNTIME_BLOCKERS.md) | Earlier blocker list | [GAP_LEDGER](GAP_LEDGER.md) |
| [SCREENSHOT_INDEX.md](../../archive/legacy-docs/docs/edai1/SCREENSHOT_INDEX.md) | Earlier screenshot list | [SCREENSHOT_AUDIT](SCREENSHOT_AUDIT.md) |
| [DATAHUB_39_TO_32_SCOPE_RECONCILIATION.md](../../archive/legacy-docs/docs/edai1/DATAHUB_39_TO_32_SCOPE_RECONCILIATION.md) | DataHub scope reconciliation | [DataHub report](datahub/DATAHUB_GOVERNANCE.md) |
| [DATAHUB_ASSERTIONS_REMEDIATION.md](../../archive/legacy-docs/docs/edai1/DATAHUB_ASSERTIONS_REMEDIATION.md) | Remediation record | [DataHub report](datahub/DATAHUB_GOVERNANCE.md) |
| [DATAHUB_DP1_DP2_DP3_LINEAGE_AND_VALIDATION.md](../../archive/legacy-docs/docs/edai1/DATAHUB_DP1_DP2_DP3_LINEAGE_AND_VALIDATION.md) | Earlier DataHub summary | [DataHub report](datahub/DATAHUB_GOVERNANCE.md) |
| [DATAHUB_EVIDENCE_LIVING_DECISION_COPY_READY.md](../../archive/legacy-docs/docs/edai1/DATAHUB_EVIDENCE_LIVING_DECISION_COPY_READY.md) | Decision note | — |
| [DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md](../../archive/legacy-docs/docs/edai1/DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md) | Earlier DataHub evidence report | [DataHub report](datahub/DATAHUB_GOVERNANCE.md) |
| [DOCKER_COMPOSE_AND_OPTIMIZATION.md](../../archive/legacy-docs/docs/edai1/DOCKER_COMPOSE_AND_OPTIMIZATION.md) | Earlier Docker stub | [Docker report](docker/DOCKER_OPTIMIZATION.md) |
| [LAKEHOUSE_CLOUD_A_B_REPORT.md](../../archive/legacy-docs/docs/edai1/LAKEHOUSE_CLOUD_A_B_REPORT.md) | Planning stub | [Storage report](storage/STORAGE_OPTIMIZATION.md) |
| [WAREHOUSE_INDEX_BENCHMARK.md](../../archive/legacy-docs/docs/edai1/WAREHOUSE_INDEX_BENCHMARK.md) | Earlier warehouse stub | [Storage report](storage/STORAGE_OPTIMIZATION.md) |
| [LEGACY_REUSE_VERIFICATION.md](../../archive/legacy-docs/docs/edai1/LEGACY_REUSE_VERIFICATION.md), [LEGACY_TO_CLOUD_REUSE_MAP.md](../../archive/legacy-docs/docs/edai1/LEGACY_TO_CLOUD_REUSE_MAP.md), [LEGACY_TO_CLOUD_SOURCE_AUDIT.md](../../archive/legacy-docs/docs/edai1/LEGACY_TO_CLOUD_SOURCE_AUDIT.md) | Migration audits from the local to the cloud platform | — |
| [SPARK_OWNER_SCHEMA_CHANGE_PROPOSAL.md](../../archive/legacy-docs/docs/edai1/SPARK_OWNER_SCHEMA_CHANGE_PROPOSAL.md) | Schema change proposal | [Schema Design](schema/SCHEMA_DESIGN.md) §5–6 |

Current cloud operating procedures are under `docs/runbooks/edai1/cloud/`.
Superseded campaign records from `docs/streaming/` are in the legacy archive.
The small number of files left at compatibility paths are referenced by source,
tests or deployment contracts and are listed in the relocation manifest.
