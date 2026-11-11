# Legacy documentation archive and relocation manifest

This archive preserves superseded reports, plans, handoffs, migration records and point-in-time campaign notes that are not part of the submission-facing documentation. Files were relocated with `git mv`; their content and history remain available.

Current technical reports live under `docs/edai1/` and `docs/edai2/`. Operational and evidence-reproduction guidance lives under `docs/runbooks/`. Runtime-referenced compatibility paths that would require application or deployment changes were deliberately left in place.

## Directory change summary

Before this reorganization, submission reports, dated campaign records,
runbooks and migration plans were mixed across `docs/`, `docs/cloud/`,
`docs/llm_agent/`, `docs/ml/`, `docs/integration/`, `docs/migration/` and
`docs/superpowers/`. The submission-facing tree is now:

```text
docs/
├── README.md
├── architecture/       # owner-reviewable SVG and Excalidraw source
├── edai1/              # current Data Engineering technical reports
├── edai2/              # current ML, LLM and Agent technical reports
├── evidence/           # unchanged evidence payloads and provenance
├── runbooks/           # current operator and reproduction procedures
└── rubric/             # internal evidence crosswalks, not technical reports
```

Compatibility files remain under `docs/cloud/`, `docs/llm_agent/`, `docs/ml/`
and `docs/streaming/` only when repository source or tests reference their exact
paths. The archive mirrors original paths beneath `archive/legacy-docs/docs/`
so provenance remains obvious.

## Markdown classification coverage

All 241 Markdown files that were under `docs/` at the baseline are accounted
for by mutually exclusive location rules:

| Classification | Count | Exhaustive location rule |
|---|---:|---|
| A — current technical documentation | 73 | Canonical reports below `docs/edai1/` and `docs/edai2/`, current runbooks, documentation indexes, and the 12 compatibility-path reports whose exact locations are referenced by source or tests |
| B — historical/superseded | 91 | Files listed as B in the relocation manifest below and now stored below `archive/legacy-docs/docs/` |
| C — duplicate | 17 | Files listed as C below; useful nonduplicated facts are already represented by the linked canonical reports, while the originals remain archived for provenance |
| D — internal tracking/evidence | 60 | The eight gap/evidence/screenshot/historical registers at the roots of `docs/edai1/` and `docs/edai2/`, `docs/rubric/rubric_audit.md`, and 51 preserved Markdown evidence records below `docs/evidence/` |

The 108 B/C Markdown files plus the 133 Markdown files that remain under
`docs/` equal the 241-file baseline. The relocation manifest also includes
non-Markdown support files moved with their historical migration package.

## Relocation manifest

| Old path | New path | Classification | Reason |
|---|---|---|---|
| `docs/DOCUMENTATION_REGISTRY.csv` | `archive/legacy-docs/docs/DOCUMENTATION_REGISTRY.csv` | D — internal tracking | Point-in-time migration registry superseded by this relocation manifest and the canonical documentation indexes. |
| `docs/assets/architecture/ewallet_platform_overview.png` | `archive/legacy-docs/docs/assets/architecture/ewallet_platform_overview.png` | B — historical/superseded | Earlier unapproved architecture image removed from the submission-facing tree and retained for provenance. |
| `docs/00_system_architecture.md` | `archive/legacy-docs/docs/00_system_architecture.md` | B — historical/superseded | Legacy top-level report superseded by the canonical subject report. |
| `docs/01_data_generator.md` | `archive/legacy-docs/docs/01_data_generator.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/02_schema_design_example.md` | `archive/legacy-docs/docs/02_schema_design_example.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/03_batch_pipeline.md` | `archive/legacy-docs/docs/03_batch_pipeline.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/04_streaming_pipeline.md` | `archive/legacy-docs/docs/04_streaming_pipeline.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/05_storage_optimization.md` | `archive/legacy-docs/docs/05_storage_optimization.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/06_airflow_orchestration.md` | `archive/legacy-docs/docs/06_airflow_orchestration.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/07_performance_benchmarks.md` | `archive/legacy-docs/docs/07_performance_benchmarks.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/08_datahub_lineage.md` | `archive/legacy-docs/docs/08_datahub_lineage.md` | C — duplicate | Earlier top-level explanation replaced by the canonical subject report. |
| `docs/edai1/DATAHUB_39_TO_32_SCOPE_RECONCILIATION.md` | `archive/legacy-docs/docs/edai1/DATAHUB_39_TO_32_SCOPE_RECONCILIATION.md` | C — duplicate | Reconciliation detail is represented by the canonical DataHub report and evidence index. |
| `docs/edai1/DATAHUB_ASSERTIONS_REMEDIATION.md` | `archive/legacy-docs/docs/edai1/DATAHUB_ASSERTIONS_REMEDIATION.md` | C — duplicate | Remediation detail is represented by the canonical DataHub report and evidence index. |
| `docs/edai1/DATAHUB_DP1_DP2_DP3_LINEAGE_AND_VALIDATION.md` | `archive/legacy-docs/docs/edai1/DATAHUB_DP1_DP2_DP3_LINEAGE_AND_VALIDATION.md` | C — duplicate | Earlier lineage summary replaced by the canonical DataHub report. |
| `docs/edai1/DATAHUB_EVIDENCE_LIVING_DECISION_COPY_READY.md` | `archive/legacy-docs/docs/edai1/DATAHUB_EVIDENCE_LIVING_DECISION_COPY_READY.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md` | `archive/legacy-docs/docs/edai1/DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md` | C — duplicate | Earlier lineage/assertion report replaced by the canonical DataHub report. |
| `docs/edai1/DOCKER_COMPOSE_AND_OPTIMIZATION.md` | `archive/legacy-docs/docs/edai1/DOCKER_COMPOSE_AND_OPTIMIZATION.md` | C — duplicate | Earlier Docker report replaced by the canonical Docker optimization report. |
| `docs/edai1/EDAI1_HANDOFF.md` | `archive/legacy-docs/docs/edai1/EDAI1_HANDOFF.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/EDAI1_RUBRIC_100_STATUS.md` | `archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_100_STATUS.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/EDAI1_RUBRIC_COVERAGE.md` | `archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_COVERAGE.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/LAKEHOUSE_CLOUD_A_B_REPORT.md` | `archive/legacy-docs/docs/edai1/LAKEHOUSE_CLOUD_A_B_REPORT.md` | C — duplicate | Earlier storage A/B stub replaced by the canonical storage report. |
| `docs/edai1/LEGACY_REUSE_VERIFICATION.md` | `archive/legacy-docs/docs/edai1/LEGACY_REUSE_VERIFICATION.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/LEGACY_TO_CLOUD_REUSE_MAP.md` | `archive/legacy-docs/docs/edai1/LEGACY_TO_CLOUD_REUSE_MAP.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/LEGACY_TO_CLOUD_SOURCE_AUDIT.md` | `archive/legacy-docs/docs/edai1/LEGACY_TO_CLOUD_SOURCE_AUDIT.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/RUBRIC_100_CROSS_WORKSTREAM.md` | `archive/legacy-docs/docs/edai1/RUBRIC_100_CROSS_WORKSTREAM.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/RUNTIME_BLOCKERS.md` | `archive/legacy-docs/docs/edai1/RUNTIME_BLOCKERS.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/SCREENSHOT_INDEX.md` | `archive/legacy-docs/docs/edai1/SCREENSHOT_INDEX.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/SPARK_OWNER_SCHEMA_CHANGE_PROPOSAL.md` | `archive/legacy-docs/docs/edai1/SPARK_OWNER_SCHEMA_CHANGE_PROPOSAL.md` | B — historical/superseded | Earlier audit, handoff, reconciliation, proposal, or duplicate report replaced by the canonical EDAI-1 report. |
| `docs/edai1/WAREHOUSE_INDEX_BENCHMARK.md` | `archive/legacy-docs/docs/edai1/WAREHOUSE_INDEX_BENCHMARK.md` | C — duplicate | Earlier warehouse benchmark stub replaced by the canonical storage report. |
| `docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md` | `archive/legacy-docs/docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/cicd_campaign_closure_handoff_20261103.md` | `archive/legacy-docs/docs/edai2/cicd_campaign_closure_handoff_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/cicd_infra_change_review_20261103.md` | `archive/legacy-docs/docs/edai2/cicd_infra_change_review_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/cicd_observability_runtime_20261103.md` | `archive/legacy-docs/docs/edai2/cicd_observability_runtime_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/cicd_rubric_evidence_20261103.md` | `archive/legacy-docs/docs/edai2/cicd_rubric_evidence_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/integration_handoff_inventory_20261103.md` | `archive/legacy-docs/docs/edai2/integration_handoff_inventory_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/llm/g3_rubric_gate_a.md` | `archive/legacy-docs/docs/edai2/llm/g3_rubric_gate_a.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/mlops/MLOPS_FEATURE_STORE.md` | `archive/legacy-docs/docs/edai2/mlops/MLOPS_FEATURE_STORE.md` | C — duplicate | Earlier feature-store report replaced by the canonical ML pipeline and serving report. |
| `docs/edai2/prompt_r33_proposal_20261103.md` | `archive/legacy-docs/docs/edai2/prompt_r33_proposal_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/prompt_registry_incident_f9_20261103.md` | `archive/legacy-docs/docs/edai2/prompt_registry_incident_f9_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/prompt_registry_m2_networkpolicy_review_20261103.md` | `archive/legacy-docs/docs/edai2/prompt_registry_m2_networkpolicy_review_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/edai2/prompt_registry_security_assessment_20261103.md` | `archive/legacy-docs/docs/edai2/prompt_registry_security_assessment_20261103.md` | B — historical/superseded | Dated proposal, handoff, incident/audit, or superseded report replaced by the canonical EDAI-2 report. |
| `docs/integration/20261031_source_consolidation.md` | `archive/legacy-docs/docs/integration/20261031_source_consolidation.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/integration/20261101_submission_integration.md` | `archive/legacy-docs/docs/integration/20261101_submission_integration.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/llm_agent/agent_platform_20261102.md` | `archive/legacy-docs/docs/llm_agent/agent_platform_20261102.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/cicd_observability.md` | `archive/legacy-docs/docs/llm_agent/cicd_observability.md` | C — duplicate | Earlier combined CI/CD and observability summary replaced by the two canonical reports. |
| `docs/llm_agent/cpu_gke_campaign_20261031.md` | `archive/legacy-docs/docs/llm_agent/cpu_gke_campaign_20261031.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/edai2_evidence_matrix_20261031.md` | `archive/legacy-docs/docs/llm_agent/edai2_evidence_matrix_20261031.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/g1_runtime_report_20261101.md` | `archive/legacy-docs/docs/llm_agent/g1_runtime_report_20261101.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/g3_rubric_evidence_20261102.md` | `archive/legacy-docs/docs/llm_agent/g3_rubric_evidence_20261102.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/implementation_handoff.md` | `archive/legacy-docs/docs/llm_agent/implementation_handoff.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/living_decision_drafts.md` | `archive/legacy-docs/docs/llm_agent/living_decision_drafts.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/manual_p0_fixes_and_cpu_smoke.md` | `archive/legacy-docs/docs/llm_agent/manual_p0_fixes_and_cpu_smoke.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/phase_abc_p0_remediation.md` | `archive/legacy-docs/docs/llm_agent/phase_abc_p0_remediation.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/phase_b_llm_gateway.md` | `archive/legacy-docs/docs/llm_agent/phase_b_llm_gateway.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/phase_c_rag.md` | `archive/legacy-docs/docs/llm_agent/phase_c_rag.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/phase_d_auth_preflight.md` | `archive/legacy-docs/docs/llm_agent/phase_d_auth_preflight.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/llm_agent/post_r1_bounded_campaign_20261031.md` | `archive/legacy-docs/docs/llm_agent/post_r1_bounded_campaign_20261031.md` | B — historical/superseded | Point-in-time campaign/handoff document retained behind the canonical LLM/Agent reports. |
| `docs/migration/DOCUMENTATION_COUNT_RECONCILIATION.md` | `archive/legacy-docs/docs/migration/DOCUMENTATION_COUNT_RECONCILIATION.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/I7_DOCUMENTATION_COUNT_RECONCILIATION.md` | `archive/legacy-docs/docs/migration/I7_DOCUMENTATION_COUNT_RECONCILIATION.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/I7_INTEGRATION_REPORT.md` | `archive/legacy-docs/docs/migration/I7_INTEGRATION_REPORT.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/I7_LIVING_DECISION_DRAFT.md` | `archive/legacy-docs/docs/migration/I7_LIVING_DECISION_DRAFT.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/I7_RUBRIC_CROSSWALK_VALIDATION.md` | `archive/legacy-docs/docs/migration/I7_RUBRIC_CROSSWALK_VALIDATION.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/LIVING_DECISION_COPY_READY.md` | `archive/legacy-docs/docs/migration/LIVING_DECISION_COPY_READY.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/PHASE_A_B0_IMPLEMENTATION_REPORT.md` | `archive/legacy-docs/docs/migration/PHASE_A_B0_IMPLEMENTATION_REPORT.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/POST_MERGE_DOCUMENTATION_VERIFICATION.md` | `archive/legacy-docs/docs/migration/POST_MERGE_DOCUMENTATION_VERIFICATION.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_COLLISION_RESOLUTION.md` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_COLLISION_RESOLUTION.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.csv` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.csv` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.md` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.sha256` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.sha256` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_MANIFEST_SCHEMA.md` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_MANIFEST_SCHEMA.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_MAPPING_VALIDATION.json` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_MAPPING_VALIDATION.json` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_MIGRATION_PREVIEW.md` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_MIGRATION_PREVIEW.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/EVIDENCE_STORAGE_POLICY.md` | `archive/legacy-docs/docs/migration/evidence/EVIDENCE_STORAGE_POLICY.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/build_evidence_migration_map.py` | `archive/legacy-docs/docs/migration/evidence/build_evidence_migration_map.py` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/evidence/validate_evidence_migration_map.py` | `archive/legacy-docs/docs/migration/evidence/validate_evidence_migration_map.py` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/phase_a/DOCUMENTATION_LINK_VALIDATION.md` | `archive/legacy-docs/docs/migration/phase_a/DOCUMENTATION_LINK_VALIDATION.md` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/phase_a/DOCUMENTATION_PATHS_I7.txt` | `archive/legacy-docs/docs/migration/phase_a/DOCUMENTATION_PATHS_I7.txt` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/phase_a/DOCUMENTATION_REGISTRY_PHASE_A_SNAPSHOT.csv` | `archive/legacy-docs/docs/migration/phase_a/DOCUMENTATION_REGISTRY_PHASE_A_SNAPSHOT.csv` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/phase_a/I7_DOCUMENTATION_VALIDATION.json` | `archive/legacy-docs/docs/migration/phase_a/I7_DOCUMENTATION_VALIDATION.json` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/phase_a/build_documentation_registry.py` | `archive/legacy-docs/docs/migration/phase_a/build_documentation_registry.py` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/migration/phase_a/validate_documentation_registry.py` | `archive/legacy-docs/docs/migration/phase_a/validate_documentation_registry.py` | B — historical/superseded | Completed migration/integration working record retained for provenance. |
| `docs/streaming/block_a.md` | `archive/legacy-docs/docs/streaming/block_a.md` | B — historical/superseded | Historical streaming experiment/supporting record retained behind the canonical Flink report. |
| `docs/streaming/flink_evidence_manifest_20261101.md` | `archive/legacy-docs/docs/streaming/flink_evidence_manifest_20261101.md` | B — historical/superseded | Historical streaming experiment/supporting record retained behind the canonical Flink report. |
| `docs/streaming/flink_local_benchmark_results_20261101.md` | `archive/legacy-docs/docs/streaming/flink_local_benchmark_results_20261101.md` | B — historical/superseded | Historical streaming experiment/supporting record retained behind the canonical Flink report. |
| `docs/streaming/generator_compatibility_20261031.md` | `archive/legacy-docs/docs/streaming/generator_compatibility_20261031.md` | B — historical/superseded | Historical streaming experiment/supporting record retained behind the canonical Flink report. |
| `docs/superpowers/plans/2026-10-16-f8-local-model-comparison.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-16-f8-local-model-comparison.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-16-f9-mlflow-experiment-registry.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-16-f9-mlflow-experiment-registry.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-21-cloud-refactor-foundation.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-21-cloud-refactor-foundation.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-21-r2-terraform-foundation.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-21-r2-terraform-foundation.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-22-r3-dataproc-delta-smoke.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-22-r3-dataproc-delta-smoke.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-22-r4-sharded-generator-gcs-bronze.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-22-r4-sharded-generator-gcs-bronze.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-24-canonical-size-remediation-tool.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-24-canonical-size-remediation-tool.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-24-cloud-feast-training-boundary.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-24-cloud-feast-training-boundary.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-24-f9-mlflow-canonical.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-24-f9-mlflow-canonical.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-24-kfp-canonical.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-24-kfp-canonical.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-26-streaming-block-a.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-26-streaming-block-a.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-29-block-b-online-inference.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-29-block-b-online-inference.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-29-one-pass-rubric-audit-implementation.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-29-one-pass-rubric-audit-implementation.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-10-31-flink-functional-correctness-hotfix.md` | `archive/legacy-docs/docs/superpowers/plans/2026-10-31-flink-functional-correctness-hotfix.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-01-benchmark-metrics-ordering.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-01-benchmark-metrics-ordering.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-01-flink-source-watermarks.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-01-flink-source-watermarks.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-01-jenkins-platform-cicd.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-01-jenkins-platform-cicd.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-01-observability-rubric-implementation.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-01-observability-rubric-implementation.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-02-coordinator-idempotency.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-02-coordinator-idempotency.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-02-edai2-accelerated-integration.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-02-edai2-accelerated-integration.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-02-edai2-agent-platform.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-02-edai2-agent-platform.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-02-g3-byo-feature-rag-specialist.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-02-g3-byo-feature-rag-specialist.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/plans/2026-11-02-unified-jenkins-runtime.md` | `archive/legacy-docs/docs/superpowers/plans/2026-11-02-unified-jenkins-runtime.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-10-16-f8-local-model-comparison-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-10-16-f8-local-model-comparison-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-10-16-f9-mlflow-experiment-registry-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-10-16-f9-mlflow-experiment-registry-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-10-21-cloud-platform-refactor-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-10-21-cloud-platform-refactor-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-10-26-streaming-block-a-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-10-26-streaming-block-a-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-01-benchmark-metrics-ordering-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-01-benchmark-metrics-ordering-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-01-flink-source-watermarks-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-01-flink-source-watermarks-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-01-jenkins-platform-cicd-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-01-jenkins-platform-cicd-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-01-observability-rubric-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-01-observability-rubric-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-02-coordinator-idempotency-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-02-coordinator-idempotency-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-02-edai2-accelerated-integration-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-02-edai2-accelerated-integration-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-02-edai2-agent-platform-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-02-edai2-agent-platform-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/superpowers/specs/2026-11-02-g3-byo-feature-rag-specialist-design.md` | `archive/legacy-docs/docs/superpowers/specs/2026-11-02-g3-byo-feature-rag-specialist-design.md` | B — historical/superseded | Planning/specification record; not submission-facing technical documentation. |
| `docs/edai1/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md` | `docs/edai1/airflow/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md`, then (2026-11-11) `archive/legacy-docs/docs/edai1/airflow/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md` | B — historical/superseded | Superseded by `docs/edai1/BATCH_PROCESSING_AND_ORCHESTRATION.md` (5GB campaign). |
| `docs/edai1/ALL_ZONES_SCHEMA.md` | `docs/edai1/schema/ALL_ZONES_SCHEMA.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/edai1/SCHEMA_DESIGN.md` | `docs/edai1/schema/SCHEMA_DESIGN.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/edai1/OPTIMIZATION_MATRIX.md` | `docs/edai1/spark/OPTIMIZATION_MATRIX.md`, then (2026-11-11) `archive/legacy-docs/docs/edai1/spark/OPTIMIZATION_MATRIX.md` | B — historical/superseded | Historical 100GB results; superseded by `docs/edai1/BATCH_PROCESSING_AND_ORCHESTRATION.md`. |
| `docs/edai1/spark/REHEARSAL_5GB_BENCHMARK.md` | `archive/legacy-docs/docs/edai1/spark/REHEARSAL_5GB_BENCHMARK.md` | B — historical/superseded | Merged into `docs/edai1/BATCH_PROCESSING_AND_ORCHESTRATION.md` (2026-11-11). |
| `docs/agent/kagent_bounded_pilot.md` | `docs/edai2/agents/kagent_bounded_pilot.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/cloud_feast_training_snapshot.md` | `docs/edai2/mlops/references/cloud_feast_training_snapshot.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/dependency_environments.md` | `docs/edai2/mlops/references/dependency_environments.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/f9_mlflow_canonical.md` | `docs/edai2/mlops/references/f9_mlflow_canonical.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/feast_offline.md` | `docs/edai2/mlops/references/feast_offline.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/fraud_feature_design.md` | `docs/edai2/mlops/references/fraud_feature_design.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/fraud_modeling.md` | `docs/edai2/mlops/references/fraud_modeling.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/fraud_training_dataset.md` | `docs/edai2/mlops/references/fraud_training_dataset.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/gke_mlops_runtime_gate.md` | `docs/edai2/mlops/references/gke_mlops_runtime_gate.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/kfp_canonical.md` | `docs/edai2/mlops/references/kfp_canonical.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/ml/mlops_serving_runbook.md` | `docs/edai2/mlops/references/mlops_serving_runbook.md` | A — current technical documentation | Current report/reference moved into its canonical subject directory. |
| `docs/cloud/canonical_100gb_readiness.md` | `docs/runbooks/edai1/cloud/canonical_100gb_readiness.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/canonical_dataproc_performance.md` | `docs/runbooks/edai1/cloud/canonical_dataproc_performance.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/canonical_size_remediation.md` | `docs/runbooks/edai1/cloud/canonical_size_remediation.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/configuration.md` | `docs/runbooks/edai1/cloud/configuration.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/core_gold_bounded_pilot.md` | `docs/runbooks/edai1/cloud/core_gold_bounded_pilot.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/dataproc_delta_smoke.md` | `docs/runbooks/edai1/cloud/dataproc_delta_smoke.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/dp2_method2_single_dag_runbook.md` | `docs/runbooks/edai1/cloud/dp2_method2_single_dag_runbook.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/gcs_delta_lake.md` | `docs/runbooks/edai1/cloud/gcs_delta_lake.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/historical_features_bounded_pilot.md` | `docs/runbooks/edai1/cloud/historical_features_bounded_pilot.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/jenkins_cicd.md` | `docs/runbooks/edai1/cloud/jenkins_cicd.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/silver_bounded_pilot.md` | `docs/runbooks/edai1/cloud/silver_bounded_pilot.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/source_dataset_generation.md` | `docs/runbooks/edai1/cloud/source_dataset_generation.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/cloud/storage_optimization.md` | `docs/runbooks/edai1/cloud/storage_optimization.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/coordinator_idempotency_runbook.md` | `docs/runbooks/edai2/llm_agent/coordinator_idempotency_runbook.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/g3_byo_coordinator_runbook.md` | `docs/runbooks/edai2/llm_agent/g3_byo_coordinator_runbook.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/g3_byo_feature_rag_specialist_runbook.md` | `docs/runbooks/edai2/llm_agent/g3_byo_feature_rag_specialist_runbook.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/g3_mcp_namespace_isolation.md` | `docs/runbooks/edai2/llm_agent/g3_mcp_namespace_isolation.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/g3_offline_source.md` | `docs/runbooks/edai2/llm_agent/g3_offline_source.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/g3_runtime_evidence_runbook.md` | `docs/runbooks/edai2/llm_agent/g3_runtime_evidence_runbook.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/mcp_gateway.md` | `docs/runbooks/edai2/llm_agent/mcp_gateway.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/rag_runbook.md` | `docs/runbooks/edai2/llm_agent/rag_runbook.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |
| `docs/llm_agent/registry_keda_prompts.md` | `docs/runbooks/edai2/llm_agent/registry_keda_prompts.md` | A — current runbook/reference | Operational or evidence-reproduction guidance moved under the canonical runbooks hierarchy. |

## Later relocations (2026-11-10, LLM documentation consolidation)

These files were created after the 241-file baseline above, so they are not part of its counts.

| Old path | New path | Classification | Reason |
|---|---|---|---|
| `docs/edai2/llm/GLOBAL_MODELCONFIG.md` | `archive/legacy-docs/docs/edai2/llm/GLOBAL_MODELCONFIG.md` | C — duplicate | Merged into `docs/edai2/llm/LLM_PLATFORM.md` §5. |
| `docs/edai2/llm/BENCHMARK_METHODOLOGY.md` | `archive/legacy-docs/docs/edai2/llm/BENCHMARK_METHODOLOGY.md` | C — duplicate | Merged into `docs/edai2/llm/BENCHMARK_RESULTS.md` §2. |
| `docs/edai2/llm/EXP_V1_V2_SAFETY_AUDIT.md` | `archive/legacy-docs/docs/edai2/llm/EXP_V1_V2_SAFETY_AUDIT.md` | B — historical/superseded | Pre-run audit; its lasting findings are in `BENCHMARK_RESULTS.md` §2.5. |
| `docs/edai2/llm/BENCHMARK_REPLAY.md` | `archive/legacy-docs/docs/edai2/llm/BENCHMARK_REPLAY.md` | B — historical/superseded | Preparation and replay record. |
| `docs/edai2/llm/vllm_cpu_o1_benchmark.md` | `archive/legacy-docs/docs/edai2/llm/vllm_cpu_o1_benchmark.md` | B — historical/superseded | Historical O1 experiment log; summarised in `BENCHMARK_RESULTS.md` §6. |
| `docs/edai2/llm/llmd_g2_routing_benchmark.md` | `archive/legacy-docs/docs/edai2/llm/llmd_g2_routing_benchmark.md` | B — historical/superseded | Historical G2 experiment log; summarised in `BENCHMARK_RESULTS.md` §6. |
| `docs/edai2/llm/BENCHMARK_EVIDENCE_MATRIX.md` | `docs/rubric/EDAI2_LLM_BENCHMARK_EVIDENCE_MATRIX.md` | D — internal tracking/evidence | Supporting evidence crosswalk, not a primary report. |
| `docs/edai2/llm/LLM_AB_DESIGN.md` | `docs/edai2/platform/LLM_AB_DESIGN.md` | A — current technical documentation | A/B documentation kept apart from the LLM platform reports. |
