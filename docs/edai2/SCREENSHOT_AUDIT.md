# EDAI-2 screenshot audit (N13 quality gate + N15 genuine captures)

Companion to [RUBRIC_EVIDENCE_INDEX.md](RUBRIC_EVIDENCE_INDEX.md).

**Classes:**

| Class | Meaning |
|---|---|
| `NATIVE_UI` | Screenshot of the real application UI, captured in the current campaigns (N9–N13, 2026-11-03/04) |
| `HISTORICAL_UI` | Screenshot of the real application UI from an earlier, identified campaign or run |
| `REAL_TERMINAL` | Screenshot of a real terminal window running real commands. `LOCAL FIXTURE` marks synthetic test fixtures |
| `DERIVED_RENDER` | An image rendered from real recorded output (JSON, logs, `kubectl`/`curl` text). It is **not** equivalent to a native UI screenshot and never counts as one |

**Method.**
1. Every `![…](…)` link in every Markdown file under this sheet's documentation folder was followed.
2. The target file was checked to exist and to decode as PNG/JPEG.
3. The image was classified, and mapped to the rubric rows it is cited for.
4. Representative images of each class were opened and inspected visually. Captions and run IDs were cross-checked against the original evidence and the provenance manifests.

**A row "qualifies"** only if an image of an acceptable class shows the literal column-D deliverable. An image that shows a failure, a local fixture, or only a derived render does not qualify on its own.

## Totals

- **74 embedded image paths, 72 distinct contents.** N15 adds nine genuine Ubuntu terminal captures. All files exist and decode; 0 broken links.

| Class | Paths |
|---|---:|
| NATIVE_UI | 57 (55 curated captures, SHA-256 verified, plus 2 read-only captures of the running cluster: kagent agents page and Grafana Explore pods). The Jenkins `edai2-agent-ci` #2 build page was removed from the curated set because its change list shows commit trailers that must not be redistributed |
| HISTORICAL_UI | 8 (MLOps campaign 2026-10-31) |
| DERIVED_RENDER | 0 |
| REAL_TERMINAL | 9 (N15 genuine live runtime) |

The former N13 `runtime/01–08` renders are no longer embedded. Their original
raw command transcripts remain supporting evidence and are explicitly labelled
as historical output, not screenshots.

### N15 additions

| Image | Class | Rows | Proof boundary |
|---|---|---|---|
| runtime/11_agents_hpa_pdb_live_runtime.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 16,19,22,25 | Live configuration; no HPA event/KEDA |
| runtime/04_mcp_rollingupdate_live_runtime.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 18 | Ready + strategy; no executed rollout/fallback |
| runtime/03_sandbox_substrate_live_runtime.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 20,27 | Pilot only |
| runtime/05_kserve_inferenceservice_live.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 15 | Ready InferenceService/predictor |
| runtime/06_kserve_v2_metadata_live.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 15 | V2 ready + model metadata |
| runtime/07_fraud_api_prediction_kserve_correlated_live.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 15 | Prediction + matching KServe ID; no parity proof |
| runtime/08a_fraud_api_health_live.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 16 | Health 200; parity flags remain false |
| runtime/08b_fraud_api_validation_422_part1.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 16,17 | HTTP 422, part 1 |
| runtime/08c_fraud_api_validation_422_part2.png | REAL_TERMINAL / GENUINE_LIVE_RUNTIME | 16,17 | HTTP 422 details, part 2 |

## Coverage of rows whose deliverable requires screenshots

| Row | Deliverable needs | Qualifying images (class) | Verdict |
|---:|---|---|---|
| 8 | Registry UI with agents | agents/01–05 (NATIVE_UI) | **QUALIFIES** |
| 9 | Airflow RAG run success + DataHub lineage | — (Jenkins RAG CD and UI-chat citations are not the literal deliverable) | **MISSING** |
| 10 | DataHub lineage/governance for RAG | — | **MISSING** |
| 11 | Airflow materialization run | Only K8s Job workloads (12c, HISTORICAL_UI) and a parity JSON view (12d); not Airflow | **MISSING** (literal) |
| 12 | Streaming push jobs succeeded + data | — | **MISSING** |
| 13 | KFP successful run | mlops/01–03 (NATIVE_UI); 02/03 KFP (HISTORICAL_UI) | **QUALIFIES** |
| 14 | Model + data versioned in MLflow | mlops/04–08 (NATIVE_UI) | **QUALIFIES** (the alias gap is functional, not a screenshot gap) |
| 15 | KServe pod + successful call | N15 live terminal: Ready predictor, V2 metadata, prediction 200 + correlated log; historical UI retained | **QUALIFIES**; parity not inferred |
| 16, 17 | Registry + multi-replica + Sandbox + UI chat | N15 live Agent/HPA/PDB, health and 422 captures; existing registry/chat UI | **PARTIAL**: no Sandbox image for business agents |
| 18 | MCP on Helm + RollingUpdate + fallback | N15 live `drift-mcp` Ready + RollingUpdate configuration; registry UI | **PARTIAL**: no executed rollout or fallback |
| 19 | Multi-replica + autoscale | N15 live 2/2 Deployments and HPA 2–3 | **PARTIAL**: no KEDA or scale event |
| 20 | Sandbox | N15 live pilot SandboxAgent/WorkerPool/gVisor | **MISSING** for business agents; pilot proof only |
| 21 | Agent on registry | agents/01 (NATIVE_UI) | **QUALIFIES** |
| 22 | Same as 16–21 for the second agent | agents/01, 09, 08; runtime/10 (NATIVE_UI) | **PARTIAL**: no Sandbox; no KEDA/scale image |
| 23, 24 | Gateway setup (YAML) + security/policy | agents/13–14, observability/49 (NATIVE_UI Tempo spans) | **QUALIFIES** (supporting; the deliverable is mainly YAML + explanation) |
| 25 | Registry + replicas + **scale-test image** + kagent chat | agents/02, 07–08; runtime/10 (NATIVE_UI) | **PARTIAL**: no scale-test image |
| 26 | Coordinator on registry | agents/02 (NATIVE_UI) | **QUALIFIES** |
| 27 | Many agents on few workers | raw transcript only | **MISSING** |
| 28 | Coverage > 90 % screenshot | — | **MISSING** |
| 29–32 | Test results | Jenkins stage views and build pages (cicd/04–06, NATIVE_UI) show suites passing, not the specific technique | **MISSING** (technique-specific) |
| 33 | CI/CD pipelines ran | cicd/07–09 (NATIVE_UI) | **QUALIFIES** |
| 34, 35 | Agent/MCP CI/CD ran | CI: cicd/02, 06 (NATIVE_UI); CD: — | **PARTIAL**: no CD image |
| 36 | Stream-job CI/CD ran | CI: cicd/05 (NATIVE_UI); CD: — | **PARTIAL** |
| 44 | Terraform ran | — (JSON record only) | **MISSING** |
| 45 | Ansible ran | — | **MISSING** |
| 46–51 | Telemetry | observability/46–51, llm/02–04 (NATIVE_UI Grafana/Loki/Tempo) | **QUALIFIES** |
| 52, 53 | A/B dashboard | — | **MISSING** |

Rows 3–7, 37–43, 54 and 55–57 require documents, configuration links or proof, not a specific screenshot. Rows 3, 4 and 7 still have native UI support (llm/01).

**Summary.** Of the 39 rows whose deliverable needs screenshots:

| Result | Count | Rows |
|---|---:|---|
| Qualify | 15 | 8, 13, 14, 15, 21, 23, 24, 26, 33, 46, 47, 48, 49, 50, 51 |
| Partial | 9 | 16, 17, 18, 19, 22, 25, 34, 35, 36 |
| Missing | 15 | 9, 10, 11, 12, 20, 27, 28, 29, 30, 31, 32, 44, 45, 52, 53 |

## Per-image classification

| Image | Renders | Class | Source / identity | Rows | Embedded in |
|---|---|---|---|---|---|
| [02_kfp_f9_pipeline_graph.png](../evidence/cloud/mlops-campaign-20261031/screenshots/02_kfp_f9_pipeline_graph.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 13 | mlops/MLOPS_FEATURE_STORE.md |
| [03_kfp_f9_run_details.png](../evidence/cloud/mlops-campaign-20261031/screenshots/03_kfp_f9_run_details.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 13 | mlops/MLOPS_FEATURE_STORE.md |
| [04_mlflow_training_run.png](../evidence/cloud/mlops-campaign-20261031/screenshots/04_mlflow_training_run.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 14 | mlops/MLOPS_FEATURE_STORE.md |
| [06_mlflow_model_registry.png](../evidence/cloud/mlops-campaign-20261031/screenshots/06_mlflow_model_registry.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 14 | mlops/MLOPS_FEATURE_STORE.md |
| [07_kserve_inferenceservice_ready.png](../evidence/cloud/mlops-campaign-20261031/screenshots/07_kserve_inferenceservice_ready.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 15 | mlops/MLOPS_FEATURE_STORE.md, mlops/ML_PIPELINE_SERVING.md |
| [11_fraud_api_prediction.png](../evidence/cloud/mlops-campaign-20261031/screenshots/11_fraud_api_prediction.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 15 | mlops/MLOPS_FEATURE_STORE.md, mlops/ML_PIPELINE_SERVING.md |
| [12c_feast_materialization_jobs.png](../evidence/cloud/mlops-campaign-20261031/screenshots/12c_feast_materialization_jobs.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 11 | mlops/MLOPS_FEATURE_STORE.md, mlops/ML_PIPELINE_SERVING.md |
| [12d_feast_online_parity_result.png](../evidence/cloud/mlops-campaign-20261031/screenshots/12d_feast_online_parity_result.png) | yes | HISTORICAL_UI | MLOps campaign 2026-10-31 | 11 | mlops/MLOPS_FEATURE_STORE.md, mlops/ML_PIPELINE_SERVING.md |
| [01_registry_catalog_agents.png](../evidence/edai2/screenshots/agents/01_registry_catalog_agents.png) | yes | NATIVE_UI | N10-E2 2026-11-04 · agentregistry v0.4.0 ewallet-prompts | 8,21,26 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [02_registry_agent_fraud_coordinator.png](../evidence/edai2/screenshots/agents/02_registry_agent_fraud_coordinator.png) | yes | NATIVE_UI | N10-E2 · agentregistry | 8,26 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [03_registry_catalog_mcp_servers.png](../evidence/edai2/screenshots/agents/03_registry_catalog_mcp_servers.png) | yes | NATIVE_UI | N10-E2 · agentregistry | 8,18 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [04_registry_catalog_prompts.png](../evidence/edai2/screenshots/agents/04_registry_catalog_prompts.png) | yes | NATIVE_UI | N10-E2 · agentregistry | 8,34 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [05_registry_deployed.png](../evidence/edai2/screenshots/agents/05_registry_deployed.png) | yes | NATIVE_UI | N10-E2 · agentregistry | 8 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [07_kagent_coordinator_prompt.png](../evidence/edai2/screenshots/agents/07_kagent_coordinator_prompt.png) | yes | NATIVE_UI | N11 2026-11-04 · session 01a1a4e8 request nl-eb00ade694f158ff85b004a35a9d1051 | 16,22,25 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [08_kagent_coordinator_answer.png](../evidence/edai2/screenshots/agents/08_kagent_coordinator_answer.png) | yes | NATIVE_UI | N11 · trace 7e651a4f38c648bd2ca3ec22e33a5d7b | 9,16,22,25 | agents/AGENTS_REGISTRY_GATEWAYS.md, novel_ideas/NOVEL_IDEAS.md, rag/RAG_PIPELINE.md |
| [09_kagent_specialist_answer.png](../evidence/edai2/screenshots/agents/09_kagent_specialist_answer.png) | yes | NATIVE_UI | N11 · request nl-d61817187a5d735cea1ba3737dce93cb | 16,22 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [10_kagent_rejection_missing_timestamp.png](../evidence/edai2/screenshots/agents/10_kagent_rejection_missing_timestamp.png) | yes | NATIVE_UI | N11 · negative N2 | 16,22 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [11_kagent_rejection_unsupported.png](../evidence/edai2/screenshots/agents/11_kagent_rejection_unsupported.png) | yes | NATIVE_UI | N11 · negative N1 | 16,22 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [12_kagent_pii_prompt_answer.png](../evidence/edai2/screenshots/agents/12_kagent_pii_prompt_answer.png) | yes | NATIVE_UI | N12 Section 4 · request nl-89422d35e28d62650705782f2f25b1f0 | 22,50 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [13_tempo_gateway_spans_table.png](../evidence/edai2/screenshots/agents/13_tempo_gateway_spans_table.png) | yes | NATIVE_UI | N12 Packet B · trace f341b410568fb649ee0f95bba50cd27d | 23,24,49 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [14_tempo_trace_waterfall.png](../evidence/edai2/screenshots/agents/14_tempo_trace_waterfall.png) | yes | NATIVE_UI | N12 Packet B · trace f341b410568fb649ee0f95bba50cd27d | 22,25,49 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [01_jenkins_dashboard.png](../evidence/edai2/screenshots/cicd/01_jenkins_dashboard.png) | yes | NATIVE_UI | N9 readback (N10-E1) · Jenkins gen 13 | 33-36 | cicd/CICD.md |
| [02_jenkins_build_history.png](../evidence/edai2/screenshots/cicd/02_jenkins_build_history.png) | yes | NATIVE_UI | N9 · builds docs#2/#3 ml-ci#3/#4 agent-ci#2 agent-negative#2 | 33-35 | testing/TESTING.md |
| [04_jenkins_ml_ci.png](../evidence/edai2/screenshots/cicd/04_jenkins_ml_ci.png) | yes | NATIVE_UI | N9 · edai2-ml-ci | 33 | testing/TESTING.md |
| [05_jenkins_platform_ci_stageview.png](../evidence/edai2/screenshots/cicd/05_jenkins_platform_ci_stageview.png) | yes | NATIVE_UI | N9 · edai2-docs-change-detection | 33-36 | cicd/CICD.md, testing/TESTING.md |
| [06_jenkins_negative_gate_stageview.png](../evidence/edai2/screenshots/cicd/06_jenkins_negative_gate_stageview.png) | yes | NATIVE_UI | N9 · edai2-agent-negative #2 (exit 42) | 34 | testing/TESTING.md |
| [07_jenkins_cd_rag_stageview.png](../evidence/edai2/screenshots/cicd/07_jenkins_cd_rag_stageview.png) | yes | NATIVE_UI | N10-E2 2026-11-04 · edai2-cd-rag-pipeline #8 | 9,33 | cicd/CICD.md |
| [08_jenkins_cd_rag_build.png](../evidence/edai2/screenshots/cicd/08_jenkins_cd_rag_build.png) | yes | NATIVE_UI | N10-E2 · edai2-cd-rag-pipeline #8 | 33 | cicd/CICD.md |
| [09_jenkins_cd_rag_console.png](../evidence/edai2/screenshots/cicd/09_jenkins_cd_rag_console.png) | yes | NATIVE_UI | N10-E2 · Cloud Build c38e6e96; rag-ingestion@f89ecfa7 | 9,33 | cicd/CICD.md, rag/RAG_PIPELINE.md |
| [01_kagent_drift_chat_modelconfig.png](../evidence/edai2/screenshots/llm/01_kagent_drift_chat_modelconfig.png) | yes | NATIVE_UI | N10-E2 2026-11-04 · kagent session 01a1a419; ModelConfig ewallet-global-llm | 3,4,7 | llm/LLM_PLATFORM.md |
| [02_grafana_tokens_per_request.png](../evidence/edai2/screenshots/llm/02_grafana_tokens_per_request.png) | yes | NATIVE_UI | N12 Gate F 2026-11-04 · Helm r37 | 3,6,50 | llm/LLM_PLATFORM.md, observability/OBSERVABILITY.md |
| [03_grafana_ttft.png](../evidence/edai2/screenshots/llm/03_grafana_ttft.png) | yes | NATIVE_UI | N12 Gate F · Helm r37 | 6,50 | observability/OBSERVABILITY.md |
| [04_grafana_generation_rtt.png](../evidence/edai2/screenshots/llm/04_grafana_generation_rtt.png) | yes | NATIVE_UI | N12 Gate F · Helm r37 | 6,50 | observability/OBSERVABILITY.md |
| [05_tempo_llm_gateway_spans.png](../evidence/edai2/screenshots/llm/05_tempo_llm_gateway_spans.png) | yes | NATIVE_UI | N12 Packet B · trace f341b410568fb649ee0f95bba50cd27d | 5,49 | llm/LLM_PLATFORM.md |
| [01_kfp_runs.png](../evidence/edai2/screenshots/mlops/01_kfp_runs.png) | yes | NATIVE_UI | N10-E2 2026-11-04 · KFP run f9-training-20261031-151638-ml2-r3 | 13 | mlops/ML_PIPELINE_SERVING.md |
| [02_kfp_run_graph.png](../evidence/edai2/screenshots/mlops/02_kfp_run_graph.png) | yes | NATIVE_UI | N10-E2 · KFP run dc29f5cc-cc9e-49ae-b861-eacddd6269b5 | 13 | mlops/ML_PIPELINE_SERVING.md |
| [03_kfp_run_details.png](../evidence/edai2/screenshots/mlops/03_kfp_run_details.png) | yes | NATIVE_UI | N10-E2 · KFP run dc29f5cc | 13 | mlops/ML_PIPELINE_SERVING.md |
| [04_mlflow_experiments.png](../evidence/edai2/screenshots/mlops/04_mlflow_experiments.png) | yes | NATIVE_UI | N10-E2 · MLflow | 14 | mlops/ML_PIPELINE_SERVING.md |
| [05_mlflow_f9_runs.png](../evidence/edai2/screenshots/mlops/05_mlflow_f9_runs.png) | yes | NATIVE_UI | N10-E2 · MLflow F9 experiment | 14 | mlops/ML_PIPELINE_SERVING.md |
| [06_mlflow_run_artifacts.png](../evidence/edai2/screenshots/mlops/06_mlflow_run_artifacts.png) | yes | NATIVE_UI | N10-E2 · run fd639926 | 14 | mlops/ML_PIPELINE_SERVING.md |
| [07_mlflow_registered_models.png](../evidence/edai2/screenshots/mlops/07_mlflow_registered_models.png) | yes | NATIVE_UI | N10-E2 · fraud-detector | 14 | mlops/ML_PIPELINE_SERVING.md |
| [08_mlflow_model_version_1.png](../evidence/edai2/screenshots/mlops/08_mlflow_model_version_1.png) | yes | NATIVE_UI | N10-E1 · fraud-detector v1 | 14 | mlops/ML_PIPELINE_SERVING.md |
| [46_fraud_api_failed_requests.png](../evidence/edai2/screenshots/observability/46_fraud_api_failed_requests.png) | yes | NATIVE_UI | N12 · 5 x 424 missing_feature | 46 | observability/OBSERVABILITY.md |
| [46_fraud_api_req_per_sec.png](../evidence/edai2/screenshots/observability/46_fraud_api_req_per_sec.png) | yes | NATIVE_UI | N12 Gate F · 30 bounded requests n12-row46-* | 46 | observability/OBSERVABILITY.md |
| [46_fraud_api_total_requests.png](../evidence/edai2/screenshots/observability/46_fraud_api_total_requests.png) | yes | NATIVE_UI | N12 · n12-row46-* | 46 | observability/OBSERVABILITY.md |
| [47_cpu.png](../evidence/edai2/screenshots/observability/47_cpu.png) | yes | NATIVE_UI | N12 Gate D · Helm r35 | 47 | observability/OBSERVABILITY.md |
| [47_disk_io.png](../evidence/edai2/screenshots/observability/47_disk_io.png) | yes | NATIVE_UI | N12 Gate D · Helm r35 | 47 | observability/OBSERVABILITY.md |
| [47_memory.png](../evidence/edai2/screenshots/observability/47_memory.png) | yes | NATIVE_UI | N12 Gate D · Helm r35 | 47 | observability/OBSERVABILITY.md |
| [47_network.png](../evidence/edai2/screenshots/observability/47_network.png) | yes | NATIVE_UI | N12 Gate D · Helm r35 | 47 | observability/OBSERVABILITY.md |
| [47_node_fs.png](../evidence/edai2/screenshots/observability/47_node_fs.png) | yes | NATIVE_UI | N12 Gate D · Helm r35 | 47 | observability/OBSERVABILITY.md |
| [47_pvc.png](../evidence/edai2/screenshots/observability/47_pvc.png) | yes | NATIVE_UI | N12 Gate D · Helm r35 | 47 | observability/OBSERVABILITY.md |
| [48_loki_agents_by_request_id.png](../evidence/edai2/screenshots/observability/48_loki_agents_by_request_id.png) | yes | NATIVE_UI | N11 · request nl-eb00ade694f158ff85b004a35a9d1051 | 48 | observability/OBSERVABILITY.md |
| [48_loki_fraud_api_kserve.png](../evidence/edai2/screenshots/observability/48_loki_fraud_api_kserve.png) | yes | NATIVE_UI | N12 Gate E · Helm r36 | 48 | observability/OBSERVABILITY.md |
| [48_loki_gateways_by_trace.png](../evidence/edai2/screenshots/observability/48_loki_gateways_by_trace.png) | yes | NATIVE_UI | N12 Gate E · trace 6de09d68d7a4b70fc9012b00e41d13d7 | 48 | observability/OBSERVABILITY.md |
| [48_loki_volume_by_service.png](../evidence/edai2/screenshots/observability/48_loki_volume_by_service.png) | yes | NATIVE_UI | N12 Gate E · Helm r36 | 48 | observability/OBSERVABILITY.md |
| [49_tempo_gateway_spans.png](../evidence/edai2/screenshots/observability/49_tempo_gateway_spans.png) | yes | NATIVE_UI | N12 Packet B · trace f341b410568fb649ee0f95bba50cd27d | 49 | observability/OBSERVABILITY.md |
| [49_tempo_trace.png](../evidence/edai2/screenshots/observability/49_tempo_trace.png) | yes | NATIVE_UI | N12 Packet B · trace f341b410568fb649ee0f95bba50cd27d | 49 | observability/OBSERVABILITY.md |
| [50_pii_by_type.png](../evidence/edai2/screenshots/observability/50_pii_by_type.png) | yes | NATIVE_UI | N12 Section 4 · same | 50 | novel_ideas/NOVEL_IDEAS.md, observability/OBSERVABILITY.md |
| [50_pii_caught.png](../evidence/edai2/screenshots/observability/50_pii_caught.png) | yes | NATIVE_UI | N12 Section 4 · request nl-89422d35e28d62650705782f2f25b1f0 | 50 | observability/OBSERVABILITY.md |
| [51_agent_calls_by_outcome.png](../evidence/edai2/screenshots/observability/51_agent_calls_by_outcome.png) | yes | NATIVE_UI | N12 Section 4 · Helm r40 | 51 | observability/OBSERVABILITY.md |
| [51_agent_calls_replacement_window.png](../evidence/edai2/screenshots/observability/51_agent_calls_replacement_window.png) | yes | NATIVE_UI | N12 G3 · F2 window, Helm r40 | 51 | novel_ideas/NOVEL_IDEAS.md, observability/OBSERVABILITY.md |
| [51_mcp_calls_per_tool.png](../evidence/edai2/screenshots/observability/51_mcp_calls_per_tool.png) | yes | NATIVE_UI | N12 G3 · Helm r40 | 51 | observability/OBSERVABILITY.md |
| [51_mcp_failures_per_tool.png](../evidence/edai2/screenshots/observability/51_mcp_failures_per_tool.png) | yes | NATIVE_UI | N12 G3 · authorized negative test n12-tool-failure | 51 | observability/OBSERVABILITY.md |
| [09_kagent_ui_agents_r41.png](../evidence/edai2/screenshots/runtime/09_kagent_ui_agents_r41.png) | yes | NATIVE_UI | N13 2026-11-04, r41 | 16,22,25 | agents/AGENTS_REGISTRY_GATEWAYS.md |
| [10_grafana_agent_pods_per_replica.png](../evidence/edai2/screenshots/runtime/10_grafana_agent_pods_per_replica.png) | yes | NATIVE_UI | N13 2026-11-04, r41 | 16,19,22,25 | agents/AGENTS_REGISTRY_GATEWAYS.md |
