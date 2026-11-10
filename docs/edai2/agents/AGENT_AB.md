# Agent configuration A/B test

This page describes the A/B test between two model configurations of the Fraud Coordinator (EDAI-2 row 52). It is implemented and tested locally with two real models. **It is not deployed**, so the row stays open until package P9 runs.

| Status | State |
|---|---|
| `IMPLEMENTED` | Yes |
| `LOCAL_VALIDATED` | Yes: real planner, pinned gateway, real vLLM with both Qwen models |
| `CLOUD_DEPLOYED` | No |
| `CLOUD_RUNTIME_VERIFIED` | No |
| `RUBRIC_EVIDENCE_COMPLETE` | No |

## Where the model is really chosen

| Agent | Runtime | Uses an LLM? | How its model is set |
|---|---|---|---|
| Fraud Coordinator | BYO | **Yes, one tool-selection call** | `EWALLET_LLM_MODEL` rendered by Helm; with `modelConfig.byoResolver` on, read from the `ModelConfig` CR at startup ([LLM_PLATFORM §5](../llm/LLM_PLATFORM.md#5-global-modelconfig-and-byo-resolver)) |
| Feature-RAG Specialist | BYO | No: deterministic MCP calls | Not applicable |
| Drift Specialist | Declarative | Yes | `ModelConfig`, fixed per Agent |

A second `ModelConfig` alone would change nothing on the BYO path, so none was added. The experiment acts on the Coordinator's call:

```text
Analyst question → Fraud Coordinator
  ↓ assign once per conversation: sha256(experiment, salt, context_id)
Variant planner: model alias, temperature, max_tokens
  ↓ agentgateway route → vLLM
Tool selection → grounding against the analyst's text
  ↓ unchanged deterministic path
A2A → Specialist → MCP → Feast, KServe, RAG → business result
```

The result artifact gains an `agent_experiment` block with the variant, the model vLLM reported and the `x-ewallet-backend` header. Without `EWALLET_AGENT_AB_CONFIG`, nothing changes.

## Variants

| | Baseline | Candidate: sampling | Candidate: model |
|---|---|---|---|
| Overlay | none | [`c7/agent-ab-cpu.yaml`](../../../infra/helm/llm-agent-platform/c7/agent-ab-cpu.yaml) | [`c7/agent-ab-qwen-1-7b.yaml`](../../../infra/helm/llm-agent-platform/c7/agent-ab-qwen-1-7b.yaml) |
| Model | Qwen3-0.6B | Qwen3-0.6B | Qwen3-1.7B |
| Temperature | 0 | 0.3 | 0 |
| Max tokens | 384 | 256 | 384 |
| Needs a GPU | No | No | Only for GPU speed |

The system prompt, tool schema, grounding, MCP grants and A2A path are shared code, and a test checks both variants send the same prompt and tools. Identical variants are refused at startup.

**Rollback:** set the weights to 100/0, or disable the overlay.

## Local result

[`Fixed suite`](../../../configs/experiments/fixtures/agent-ab-suite.jsonl): 20 business cases and 4 off-topic cases, each run on both variants in alternating order, through the production `plan` path, the pinned gateway and real vLLM. Evidence: [`summary.json`](../../../docs/evidence/edai2/llm_ab/local_real_models_20261106T192621Z/agent_suite/summary.json).

| Metric | Baseline, Qwen3-0.6B | Candidate, Qwen3-1.7B |
|---|---:|---:|
| Task success | 23/24 | 24/24 |
| Correct tool | 23/24 | 24/24 |
| Invalid tool calls | 0 | 0 |
| Invalid arguments | 1 | 0 |
| LLM errors | 0 | 0 |
| Tool calls | 20 | 20 |
| Prompt tokens | 13,317 | 13,317 |
| Completion tokens | 5,330 | 5,346 |

**Reading:** the variants differ on one case. The 0.6B model called the tool for an off-topic request, and grounding refused it, so nothing ran. One case in 24 does not show that either model is better. Latency is not reported as a comparison, because the two models ran on 4 and 6 CPU cores of a laptop.

A first run was discarded. Its 20-second timeouts evicted the slow backends, and a stub fallback then answered. The harness now routes fallback to a real backend.

## Metrics

| Rubric metric | Source |
|---|---|
| Task success, correct tool, invalid calls | `ops/experiments/agent_ab_eval.py`, paired on the fixed suite |
| Tool calls, latency, tokens, errors | Same, and `ewallet_agent_ab_*` on the cluster |
| Resource use | Pod CPU and memory for the Coordinator and vLLM |
| Model and backend attribution | `agent_experiment` artifact, audit fields, span attributes |

Panels are in the [A/B dashboard](../../../infra/helm/llm-agent-platform/dashboards/ab-experiments.json), shipped only when an experiment is on.

## Limits

- **Drift Specialist is not in the experiment.** `ModelConfig` is fixed per Agent, so a per-request variant needs a second Agent with its own ServiceAccount, MCP credentials and A2A route. That is more security surface than this comparison justifies.
- **Local runs stop at the LLM decision.** MCP tools, Feast and KServe through A2A need the cluster.
- **Assignment is per conversation.** The suite forces both variants on each case; live traffic follows the hash.
- **Token budget.** Production allows 24,000 tokens an hour per model name, and a suite pass uses about 18,600 per variant. Use `c7/experiment-limits.yaml` during an approved session.

Deployment is package P9 in the [approval packages](../../runbooks/edai2/C_C_APPROVAL_PACKAGES.md).
