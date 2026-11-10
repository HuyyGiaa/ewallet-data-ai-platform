# EDAI-2 Historical Documents

The files below are internal working documents produced during development: proposals, change reviews, incident records, handoffs and earlier rubric maps. They are kept unchanged for traceability, but they are **not** the submission reports. Several of them describe infrastructure assumptions or states that were later superseded, for example "registry not deployed" or "streaming not supported", so they must not be read as the current system state. The current technical reports are listed in the [EDAI-2 index](README.md).

These files were moved with `git mv` to `archive/legacy-docs/`; their content
and Git history remain available. The complete old-to-new path map is in the
[relocation manifest](../../archive/legacy-docs/README.md).

| File | Kind | Current report |
|---|---|---|
| [llm/g3_rubric_gate_a.md](../../archive/legacy-docs/docs/edai2/llm/g3_rubric_gate_a.md) | Planning and approval gate (rubric map) | [LLM Platform](llm/LLM_PLATFORM.md), [Agents](agents/AGENTS_REGISTRY_GATEWAYS.md) |
| [mlops/MLOPS_FEATURE_STORE.md](../../archive/legacy-docs/docs/edai2/mlops/MLOPS_FEATURE_STORE.md) | Earlier MLOps campaign report with status vocabulary | [ML Pipeline and Serving](mlops/ML_PIPELINE_SERVING.md) |
| [cicd_rubric_evidence_20261103.md](../../archive/legacy-docs/docs/edai2/cicd_rubric_evidence_20261103.md) | Earlier CI/CD rubric evidence | [CI/CD](cicd/CICD.md) |
| [cicd_campaign_closure_handoff_20261103.md](../../archive/legacy-docs/docs/edai2/cicd_campaign_closure_handoff_20261103.md) | Campaign handoff | [CI/CD](cicd/CICD.md) |
| [cicd_infra_change_review_20261103.md](../../archive/legacy-docs/docs/edai2/cicd_infra_change_review_20261103.md) | Change review | [CI/CD](cicd/CICD.md) |
| [cicd_observability_runtime_20261103.md](../../archive/legacy-docs/docs/edai2/cicd_observability_runtime_20261103.md) | Earlier observability readback | [Observability](observability/OBSERVABILITY.md) |
| [batch_d_mcp_agent_cd_proposal_20261103.md](../../archive/legacy-docs/docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md) | Deployment proposal (MCP/agent CD) | [CI/CD §6](cicd/CICD.md#6-limitations) |
| [integration_handoff_inventory_20261103.md](../../archive/legacy-docs/docs/edai2/integration_handoff_inventory_20261103.md) | Integration handoff | — |
| [prompt_r33_proposal_20261103.md](../../archive/legacy-docs/docs/edai2/prompt_r33_proposal_20261103.md) | Prompt-management proposal | [CI/CD §5](cicd/CICD.md#5-mcp-servers-agents-and-prompts) |
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

Two documents moved instead of being archived: the evidence matrix is now [`docs/rubric/EDAI2_LLM_BENCHMARK_EVIDENCE_MATRIX.md`](../rubric/EDAI2_LLM_BENCHMARK_EVIDENCE_MATRIX.md), and the LLM A/B design is now [`platform/LLM_AB_DESIGN.md`](platform/LLM_AB_DESIGN.md). Frozen evidence READMEs whose checksums are recorded still name the old paths; use this table to follow them.

**Runbooks and compatibility paths.** Current agent runbooks are under
`docs/runbooks/edai2/llm_agent/`, and current MLOps references are under
`docs/edai2/mlops/references/`. Files that remain in `docs/llm_agent/` or
`docs/ml/` are retained at compatibility paths because source, tests or
deployment contracts reference them. Superseded phase notes are in the legacy
archive.
