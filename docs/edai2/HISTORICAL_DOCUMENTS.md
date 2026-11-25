# EDAI-2 Historical Documents

The files below are internal working documents produced during development: proposals, change reviews, incident records, and handoffs. They are kept unchanged for traceability, but they are **not** the submission reports. Several of them describe infrastructure assumptions or states that were later superseded, for example "registry not deployed" or "streaming not supported", so they must not be read as the current system state. The current technical reports are listed in the [EDAI-2 index](README.md).

These files were moved with `git mv` to `archive/legacy-docs/`; their content
and Git history remain available. The complete old-to-new path map is in the
[relocation manifest](../../archive/legacy-docs/README.md).

| File | Kind | Current report |
|---|---|---|
| [mlops/MLOPS_FEATURE_STORE.md](../../archive/legacy-docs/docs/edai2/mlops/MLOPS_FEATURE_STORE.md) | Earlier MLOps campaign report with status vocabulary | [ML Pipeline and Serving](mlops/ML_PIPELINE_SERVING.md) |
| [cicd_campaign_closure_handoff_20261103.md](../../archive/legacy-docs/docs/edai2/cicd_campaign_closure_handoff_20261103.md) | Campaign handoff | [CI/CD architecture](cicd/CICD_ARCHITECTURE.md) |
| [cicd_infra_change_review_20261103.md](../../archive/legacy-docs/docs/edai2/cicd_infra_change_review_20261103.md) | Change review | [CI/CD architecture](cicd/CICD_ARCHITECTURE.md) |
| [cicd_observability_runtime_20261103.md](../../archive/legacy-docs/docs/edai2/cicd_observability_runtime_20261103.md) | Earlier observability readback | [Observability](observability/OBSERVABILITY.md) |
| [batch_d_mcp_agent_cd_proposal_20261103.md](../../archive/legacy-docs/docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md) | Deployment proposal (MCP/agent CD) | [CI/CD architecture §10](cicd/CICD_ARCHITECTURE.md#10-the-production-boundary) |
| [integration_handoff_inventory_20261103.md](../../archive/legacy-docs/docs/edai2/integration_handoff_inventory_20261103.md) | Integration handoff | — |
| [prompt_r33_proposal_20261103.md](../../archive/legacy-docs/docs/edai2/prompt_r33_proposal_20261103.md) | Prompt-management proposal | [CI/CD architecture §9.1](cicd/CICD_ARCHITECTURE.md#91-registry-gate-prompt-management) |
| [prompt_registry_incident_f9_20261103.md](../../archive/legacy-docs/docs/edai2/prompt_registry_incident_f9_20261103.md) | Incident record | — |
| [prompt_registry_m2_networkpolicy_review_20261103.md](../../archive/legacy-docs/docs/edai2/prompt_registry_m2_networkpolicy_review_20261103.md) | Security review | [Infrastructure and Security](platform/GATEWAY_IAC_SECURITY_AB.md) |
| [prompt_registry_security_assessment_20261103.md](../../archive/legacy-docs/docs/edai2/prompt_registry_security_assessment_20261103.md) | Security assessment | [Infrastructure and Security](platform/GATEWAY_IAC_SECURITY_AB.md) |

**LLM documents archived on 2026-11-10.** The LLM documentation was consolidated into two reports, [LLM_PLATFORM.md](llm/LLM_PLATFORM.md) and [BENCHMARK_RESULTS.md](llm/BENCHMARK_RESULTS.md). The following earlier documents were moved with `git mv` and are kept for traceability only:

| File | Kind | Current report |
|---|---|---|
| [llm/GLOBAL_MODELCONFIG.md](../../archive/legacy-docs/docs/edai2/llm/GLOBAL_MODELCONFIG.md) | Detailed ModelConfig and BYO resolver report, merged | [LLM Platform §5](llm/LLM_PLATFORM.md#5-global-modelconfig-and-byo-resolver) |
| [llm/BENCHMARK_METHODOLOGY.md](../../archive/legacy-docs/docs/edai2/llm/BENCHMARK_METHODOLOGY.md) | Benchmark methodology, merged; also lists the agent experiments | [Benchmark Results §2](llm/BENCHMARK_RESULTS.md#2-experimental-environment-and-methodology) |
| [llm/EXP_V1_V2_SAFETY_AUDIT.md](../../archive/legacy-docs/docs/edai2/llm/EXP_V1_V2_SAFETY_AUDIT.md) | Pre-run safety audit, written before the experiments ran | [Benchmark Results §2.5](llm/BENCHMARK_RESULTS.md#25-observability-and-safety-of-the-sessions) |
| [llm/BENCHMARK_REPLAY.md](../../archive/legacy-docs/docs/edai2/llm/BENCHMARK_REPLAY.md) | Replay design, GKE smoke validation and stage durations | [Benchmark Results §9](llm/BENCHMARK_RESULTS.md#9-evidence-and-reproducibility) |
| [llm/vllm_cpu_o1_benchmark.md](../../archive/legacy-docs/docs/edai2/llm/vllm_cpu_o1_benchmark.md) | Complete O1 experiment log (historical) | [Benchmark Results §6](llm/BENCHMARK_RESULTS.md#6-comparative-analysis) |
| [llm/llmd_g2_routing_benchmark.md](../../archive/legacy-docs/docs/edai2/llm/llmd_g2_routing_benchmark.md) | Complete G2 experiment log (historical) | [Benchmark Results §6](llm/BENCHMARK_RESULTS.md#6-comparative-analysis) |

The LLM A/B design moved instead of being archived and is now [`platform/LLM_AB_DESIGN.md`](platform/LLM_AB_DESIGN.md). Frozen evidence READMEs whose checksums are recorded still name the old paths; use this table to follow them.

**Session handoffs archived on 2026-11-25.** These internal handoff and integration-planning notes were moved with `git mv`; the reports they fed are the current ones.

| File | Kind | Current report |
|---|---|---|
| [platform/SESSION_A_HANDOFF.md](../../archive/legacy-docs/docs/edai2/platform/SESSION_A_HANDOFF.md) | Session A handoff (Feature API, Locust, KEDA, edge) | [Serving Autoscaling with KEDA](mlops/SERVING_AUTOSCALING_KEDA.md), [External Gateway](gateway/EXTERNAL_GATEWAY_ARCHITECTURE.md) |
| [platform/C_B_HANDOFF.md](../../archive/legacy-docs/docs/edai2/platform/C_B_HANDOFF.md), [platform/C_C_HANDOFF.md](../../archive/legacy-docs/docs/edai2/platform/C_C_HANDOFF.md) | Batch C-B and C-C handoffs | [CI/CD architecture](cicd/CICD_ARCHITECTURE.md), [Vault](platform/VAULT.md) |
| [platform/C_DATA_PLATFORM_INTEGRATION.md](../../archive/legacy-docs/docs/edai2/platform/C_DATA_PLATFORM_INTEGRATION.md), [platform/C_INTEGRATION_ORDER.md](../../archive/legacy-docs/docs/edai2/platform/C_INTEGRATION_ORDER.md) | Integration contract and merge order | [Feature Store](mlops/FEATURE_STORE.md) |
| [testing/B1_TESTING_QUALITY.md](../../archive/legacy-docs/docs/edai2/testing/B1_TESTING_QUALITY.md) | B1 testing work record and test catalogue | [Testing](testing/TESTING.md) |

**Runbooks and compatibility paths.** Current agent runbooks are under
`docs/runbooks/edai2/llm_agent/`, and current MLOps references are under
`docs/edai2/mlops/references/`. Files that remain in `docs/llm_agent/` or
`docs/ml/` are retained at compatibility paths because source, tests or
deployment contracts reference them. Superseded phase notes are in the legacy
archive.
