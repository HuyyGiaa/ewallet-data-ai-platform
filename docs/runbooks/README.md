# Runbook index

This index points to the operational procedures of the platform: how to run,
verify and reproduce each component. The technical reports that explain the
components are indexed in [EDAI-1](../edai1/README.md) and [EDAI-2](../edai2/README.md).

## EDAI-1 and shared data platform

| Component | Runbook | Executes | Prerequisites and runtime | Notes |
|---|---|---|---|---|
| Platform configuration | [Runtime configuration](../cloud/configuration.md) | Profile loading and preflight | Local Python plus configured GCP/local services | Bounded, approval-gated procedure |
| Source generation | [Cloud source generation](../cloud/source_dataset_generation.md) | Bounded/canonical source planning and generation | GCS/GCP identity, manifest, cost gate | Bounded, approval-gated procedure |
| Dataproc compatibility | [Delta-on-GCS smoke](../cloud/dataproc_delta_smoke.md) | Dry-run and bounded Managed Spark smoke | gcloud, Dataproc quota, isolated GCS prefix | Run record |
| DP2 Method 2 | [Single-DAG DP2 runbook](../cloud/dp2_method2_single_dag_runbook.md) | Silver/Gold baseline and optimized orchestration | Airflow 3, frozen manifests, quota/cost gates | Bounded, approval-gated procedure |
| Storage optimization | [Cloud storage optimization](../cloud/storage_optimization.md) | Storage A/B planning/validation | Immutable inputs and isolated outputs | Bounded, approval-gated procedure |
| Terraform foundation | [GCP foundation](../cloud/terraform_gcp_foundation.md) | Terraform plan/apply/cleanup workflow | Remote state, GCP auth, explicit mutation approval | Bounded, approval-gated procedure |
| DataHub | [DP1-DP3 lineage and validation](../edai1/DATAHUB_DP1_DP2_DP3_LINEAGE_AND_VALIDATION.md) | Metadata/assertion publication and readback context | DataHub GMS and exact cloud-derived URNs | See also the DataHub reproduction runbook |

Bounded Silver, Gold, and historical-feature documents remain linked from the
[EDAI-1 index](../edai1/README.md) as historical run records rather than current
generic runbooks.

## EDAI-2

| Component | Runbook | Executes | Prerequisites and runtime | Notes |
|---|---|---|---|---|
| Feast/PIT boundary | [Cloud Feast training snapshot](../ml/cloud_feast_training_snapshot.md) | Registry readback, PIT validation, bounded snapshot workflow | Immutable registry/data manifests and explicit cloud gate | Bounded, approval-gated procedure |
| ML training snapshot | [ML2 operator runbook](../ml/ml2_training_snapshot_operator_runbook.md) | Source-controlled snapshot/training boundary | Feast gates, exact Git/data identity | Bounded, approval-gated procedure |
| MLflow/F9 | [Canonical F9 and MLflow](../ml/f9_mlflow_canonical.md) | Four-model experiment and tracking | Complete snapshot, MLflow, image identity | Bounded, approval-gated procedure |
| KFP | [Canonical standalone KFP](../ml/kfp_canonical.md) | Compile/submit/verify KFP pipeline | Healthy standalone KFP and explicit approval | Bounded, approval-gated procedure |
| GKE MLOps | [Runtime gate](../ml/gke_mlops_runtime_gate.md) | Deploy/validate PostgreSQL, MLflow, KFP | GKE access/capacity and mutation gate | Bounded, approval-gated procedure |
| LLM/Agent platform | [Runtime runbook](../llm_agent/runtime_runbook.md) | LLM, gateway, MCP/Agent bounded operations | Pinned images, security preflight, exclusive GKE window | Bounded, approval-gated procedure |
| LLM P0 smoke | [Manual fixes and CPU smoke](../llm_agent/manual_p0_fixes_and_cpu_smoke.md) | Local/CPU smoke sequence | Local runtime and exact source state | Earlier procedure |
| kagent | [Bounded pilot](../agent/kagent_bounded_pilot.md) | kagent source/runtime pilot | Agent platform prerequisites | Earlier pilot |
| EDAI-2 UI evidence | [UI replay](edai2/UI_REPLAY.md) | Read-only port-forwards, runtime renders, Grafana panels | GKE access from an authorized network | Read-only access to the cloud UIs |

RAG ingestion: [RAG runbook](../llm_agent/rag_runbook.md). DataHub assertions: [reproduction runbook](DATAHUB_ASSERTIONS_REPRODUCIBILITY.md).

## Operating rules

1. Re-read the linked file and current `--help` before execution.
2. Treat historical IDs, paths, costs, and screenshots as evidence, not reusable defaults.
3. Require exact Git SHA, data generation/checksum, run ID, and isolated output path.
4. Use dry-run/read-only gates before any mutation.
5. Do not infer runtime success from source readiness or a unit-test result.
