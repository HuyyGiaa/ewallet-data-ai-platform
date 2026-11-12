# Runbook index

This index points to the operational procedures of the platform: how to run,
verify and reproduce each component. The technical reports that explain the
components are indexed in [EDAI-1](../edai1/README.md) and [EDAI-2](../edai2/README.md).

## EDAI-1 and shared data platform

| Component | Runbook | Executes | Prerequisites and runtime | Notes |
|---|---|---|---|---|
| Platform configuration | [Runtime configuration](edai1/cloud/configuration.md) | Profile loading and preflight | Local Python plus configured GCP/local services | Bounded, approval-gated procedure |
| EDAI-1 DP1–DP3 campaign (current) | [EDAI-1 pipeline runbook](edai1/cloud/edai1_campaign_runbook.md) | `rehearsal-5gb-v1` source generation, DP1–DP3 Airflow DAGs on Dataproc, validation, campaign comparison | GCP identity, the 5GB data bucket, Airflow 3, cost gates | Current workflow; campaign `ab-20261110225456` |
| Dataproc compatibility | [Delta-on-GCS smoke](edai1/cloud/dataproc_delta_smoke.md) | Dry-run and bounded Managed Spark smoke | gcloud, Dataproc quota, isolated GCS prefix | Run record |
| Docker, lakehouse and warehouse optimization | [Owner evidence runbook](edai1/DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md) | Docker A/B and Compose; PostgreSQL index benchmark on real 5GB Gold rows; isolated Delta compaction/Z-order/pruning benchmark | Docker, isolated PostgreSQL, schema-viz Trino, one owner-approved Dataproc batch | Run 2026-11-12/13 (`20261112t142522`); evidence integrated |
| Lakehouse storage benchmark | [Cloud Delta storage optimization](edai1/cloud/storage_optimization.md) | Plan, dry run, paid batch, Trino verification and the canonical ST01–ST09 SQL | gcloud ADC, Dataproc quota, a verified campaign and its workload spec | Bounded, approval-gated procedure |
| Terraform foundation | [GCP foundation](../cloud/terraform_gcp_foundation.md) | Terraform plan/apply/cleanup workflow | Remote state, GCP auth, explicit mutation approval | Bounded, approval-gated procedure |
| Schema visualization | [DBeaver and Trino](edai1/SCHEMA_VISUALIZATION_DBEAVER.md) | Read-only Trino over the canonical GCS runs; DBeaver zone views and Gold ERD | Docker, ADC with bucket read access, DBeaver | Owner captures screenshots E01–E08 |
| DataHub | [Assertion reproduction](DATAHUB_ASSERTIONS_REPRODUCIBILITY.md) | Metadata/assertion publication and readback | DataHub GMS and exact cloud-derived URNs | Historical campaign context is retained in the legacy archive |

Superseded EDAI-1 procedures stay under `edai1/cloud/` for provenance, each marked
*Historical* at the top: the 1 GB pilot runbooks (source generation, GCS layout,
bounded Silver, core Gold and historical features), the `canonical-100gb-v1`
readiness, performance and size-remediation runbooks, and the single-DAG DP2
runbook. They are not part of the current 5GB workflow.

## EDAI-2

| Component | Runbook | Executes | Prerequisites and runtime | Notes |
|---|---|---|---|---|
| Feast/PIT boundary | [Cloud Feast training snapshot](../edai2/mlops/references/cloud_feast_training_snapshot.md) | Registry readback, PIT validation, bounded snapshot workflow | Immutable registry/data manifests and explicit cloud gate | Bounded, approval-gated procedure |
| ML training snapshot | [ML2 operator runbook](../ml/ml2_training_snapshot_operator_runbook.md) | Source-controlled snapshot/training boundary | Feast gates, exact Git/data identity | Bounded, approval-gated procedure |
| MLflow/F9 | [Canonical F9 and MLflow](../edai2/mlops/references/f9_mlflow_canonical.md) | Four-model experiment and tracking | Complete snapshot, MLflow, image identity | Bounded, approval-gated procedure |
| Agent platform and benchmarks | [C-B runbook](edai2/llm_agent/C_AGENT_PLATFORM_RUNBOOK.md) | Approval packages P1–P6: Jenkins CD, EXP-V1/V2, KEDA, isolated MCP, Sandbox | Owner approval per package | Not yet run on the cluster |
| KFP | [Canonical standalone KFP](../edai2/mlops/references/kfp_canonical.md) | Compile/submit/verify KFP pipeline | Healthy standalone KFP and explicit approval | Bounded, approval-gated procedure |
| GKE MLOps | [Runtime gate](../edai2/mlops/references/gke_mlops_runtime_gate.md) | Deploy/validate PostgreSQL, MLflow, KFP | GKE access/capacity and mutation gate | Bounded, approval-gated procedure |
| LLM/Agent platform | [Runtime runbook](../llm_agent/runtime_runbook.md) | LLM, gateway, MCP/Agent bounded operations | Pinned images, security preflight, exclusive GKE window | Bounded, approval-gated procedure |
| LLM P0 smoke | [Manual fixes and CPU smoke](../../archive/legacy-docs/docs/llm_agent/manual_p0_fixes_and_cpu_smoke.md) | Local/CPU smoke sequence | Local runtime and exact source state | Earlier procedure |
| kagent | [Bounded pilot](../edai2/agents/kagent_bounded_pilot.md) | kagent source/runtime pilot | Agent platform prerequisites | Earlier pilot |
| Data platform | [C1 to C3 runbook](edai2/C_DATA_PLATFORM_RUNBOOK.md) | Airflow Feast materialization, Flink dual-store verification, Gold equivalence, schema diagrams, Spark UI | Mixed; every step is marked read-only or mutating | Cloud steps need owner approval |
| EDAI-2 UI evidence | [UI replay](edai2/UI_REPLAY.md) | Read-only port-forwards, runtime renders, Grafana panels | GKE access from an authorized network | Read-only access to the cloud UIs |
| B1 testing quality | [B1 testing runbook](edai2/B1_TESTING_RUNBOOK.md) | Coverage, Hypothesis, mutmut and Locust runs and evidence capture | uv; local only, no cloud access | Final results come from the owner's runs |

RAG ingestion: [RAG runbook](edai2/llm_agent/rag_runbook.md). DataHub assertions: [reproduction runbook](DATAHUB_ASSERTIONS_REPRODUCIBILITY.md).

## Operating rules

1. Re-read the linked file and current `--help` before execution.
2. Treat historical IDs, paths, costs, and screenshots as evidence, not reusable defaults.
3. Require exact Git SHA, data generation/checksum, run ID, and isolated output path.
4. Use dry-run/read-only gates before any mutation.
5. Do not infer runtime success from source readiness or a unit-test result.
