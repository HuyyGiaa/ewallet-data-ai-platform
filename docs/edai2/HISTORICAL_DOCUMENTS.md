# EDAI-2 Historical Documents

The files below are internal working documents produced during development: proposals, change reviews, incident records, handoffs and earlier rubric maps. They are kept unchanged for traceability, but they are **not** the submission reports. Several of them describe infrastructure assumptions or states that were later superseded, for example "registry not deployed" or "streaming not supported", so they must not be read as the current system state. The current technical reports are listed in the [EDAI-2 index](README.md).

None of these files has been moved or deleted. They are candidates for a later archive folder.

| File | Kind | Current report |
|---|---|---|
| [llm/g3_rubric_gate_a.md](llm/g3_rubric_gate_a.md) | Planning and approval gate (rubric map) | [LLM Platform](llm/LLM_PLATFORM.md), [Agents](agents/AGENTS_REGISTRY_GATEWAYS.md) |
| [mlops/MLOPS_FEATURE_STORE.md](mlops/MLOPS_FEATURE_STORE.md) | Earlier MLOps campaign report with status vocabulary | [ML Pipeline and Serving](mlops/ML_PIPELINE_SERVING.md) |
| [cicd_rubric_evidence_20261103.md](cicd_rubric_evidence_20261103.md) | Earlier CI/CD rubric evidence | [CI/CD](cicd/CICD.md) |
| [cicd_campaign_closure_handoff_20261103.md](cicd_campaign_closure_handoff_20261103.md) | Campaign handoff | [CI/CD](cicd/CICD.md) |
| [cicd_infra_change_review_20261103.md](cicd_infra_change_review_20261103.md) | Change review | [CI/CD](cicd/CICD.md) |
| [cicd_observability_runtime_20261103.md](cicd_observability_runtime_20261103.md) | Earlier observability readback | [Observability](observability/OBSERVABILITY.md) |
| [batch_d_mcp_agent_cd_proposal_20261103.md](batch_d_mcp_agent_cd_proposal_20261103.md) | Deployment proposal (MCP/agent CD) | [CI/CD §6](cicd/CICD.md#6-limitations) |
| [integration_handoff_inventory_20261103.md](integration_handoff_inventory_20261103.md) | Integration handoff | — |
| [prompt_r33_proposal_20261103.md](prompt_r33_proposal_20261103.md) | Prompt-management proposal | [CI/CD §5](cicd/CICD.md#5-mcp-servers-agents-and-prompts) |
| [prompt_registry_incident_f9_20261103.md](prompt_registry_incident_f9_20261103.md) | Incident record | — |
| [prompt_registry_m2_networkpolicy_review_20261103.md](prompt_registry_m2_networkpolicy_review_20261103.md) | Security review | [Infrastructure and Security](platform/GATEWAY_IAC_SECURITY_AB.md) |
| [prompt_registry_security_assessment_20261103.md](prompt_registry_security_assessment_20261103.md) | Security assessment | [Infrastructure and Security](platform/GATEWAY_IAC_SECURITY_AB.md) |

**Detailed experiment records.** [llm/vllm_cpu_o1_benchmark.md](llm/vllm_cpu_o1_benchmark.md) and [llm/llmd_g2_routing_benchmark.md](llm/llmd_g2_routing_benchmark.md) are complete experiment logs. Their results are summarised in the [LLM Platform report](llm/LLM_PLATFORM.md#5-performance-and-optimisation), which links to them as appendices.

**Older runbooks and design notes.** Files under `docs/llm_agent/` and `docs/ml/` (for example `agents.md`, `registry_keda_prompts.md`, `phase_*` and `g3_*` runbooks) describe earlier phases. The current reports reference them only for specific operating procedures or raw evidence.
