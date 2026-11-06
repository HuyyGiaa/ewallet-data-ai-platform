# LLM A/B test and GPU plan

This page covers the comparison of Qwen3-0.6B and Qwen3-1.7B (EDAI-2 row 53). Routing and serving are tested locally with both real models. **Nothing has run on GKE or on a GPU.** The GPU quota blocks every GPU stage.

| Status | State |
|---|---|
| `IMPLEMENTED` | Routes, candidate vLLM, GPU image recipe, staged driver, GPU sampler |
| `LOCAL_VALIDATED` | Yes: pinned gateway, real vLLM on CPU, both models live at once |
| `CLOUD_DEPLOYED` | No |
| `CLOUD_RUNTIME_VERIFIED` | No |
| `RUBRIC_EVIDENCE_COMPLETE` | No |

## Blocker

| Quota | Used / limit |
|---|---|
| `GPUS_ALL_REGIONS` | 0 / **0** |
| `NVIDIA_L4_GPUS` (asia-southeast1) | 0 / 1 |
| `PREEMPTIBLE_NVIDIA_L4_GPUS` (asia-southeast1) | 0 / 1 |
| `NVIDIA_T4_GPUS` (asia-southeast1) | 0 / 1 |
| `CPUS_ALL_REGIONS` | 16 / 32 |

The global GPU limit of 0 overrides the regional limits. Read again on 2026-11-07, unchanged. The billing account's Free Trial status is unverified, and a trial account cannot usually raise GPU quota.

**Owner decision:** requesting `GPUS_ALL_REGIONS = 1` is approved in principle, with L4 Spot preferred, a 3-hour budget target and a 6-hour session maximum. The owner handles the quota request. Until it's granted, no GPU resource exists or may be created.

## Two experiments

| | EXP-V3A | EXP-V3B |
|---|---|---|
| Question | What does the larger model cost on the same hardware? | Can one gateway run two arms live, with attribution? |
| Arms | Qwen3-0.6B, then Qwen3-1.7B | Qwen3-0.6B on CPU, Qwen3-1.7B on L4 |
| Timing | Sequential, one L4 | Concurrent, weighted |
| Fair? | Yes, only the model differs | **No, model and hardware differ together** |

**EXP-V3B has a built-in confounder.** It shows that routing and attribution work, and it may show which deployed arm serves users better. It cannot say why one arm is better, and it is not a serving optimization or an llm-d routing experiment.

## Models

| | Model A | Model B |
|---|---|---|
| Model | Qwen3-0.6B | Qwen3-1.7B |
| Revision | `c1899de289a04d12100db370d81485cdf75e47ca` | `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` |
| Architecture | `Qwen3ForCausalLM` | `Qwen3ForCausalLM` |
| Served name | `ewallet-fraud-llm-v1` | `ewallet-fraud-llm-1-7b` |

## Routing

agentgateway v1.7.0 supports weighted routing. The chart adds a client-facing name, `ewallet-llm-ab`, beside the unchanged `ewallet-fraud-llm` route. Each response carries `x-ewallet-backend`, and the body carries the model vLLM really served. Without `modelExperiments.enabled`, the chart renders byte for byte as before.

Production BYO agents read `EWALLET_LLM_MODEL` and do not use `ModelConfig`, so a change to `ModelConfig` would not move their traffic.

The split is per request, so a conversation can reach both models. Sticky per-conversation assignment is what the [Agent A/B](../agents/AGENT_AB.md) adds.

## Local results

| Check | Result | Evidence |
|---|---|---|
| Chart-rendered config on the pinned gateway, stub backends, 1,000 requests | Exactly 900 / 100, 0 failed | [routing](../../evidence/edai2/llm_ab/local_routing_20261106T185634Z.json) |
| Candidate lost, 60 requests | 1 × 503, then 59 served by primary | Same |
| Both real models live, 40 requests | 37 / 3, 0 failed, each response from its own model | [routing](../../evidence/edai2/llm_ab/local_real_models_20261106T192621Z/routing.json) |
| Qwen3-1.7B on vLLM 0.32.0 with hermes tool calling | Serves; 20 of 20 business cases called the tool correctly | [suite](../../evidence/edai2/llm_ab/local_real_models_20261106T192621Z/agent_suite/summary.json) |
| Qwen3-1.7B memory on CPU | 7.75 GiB with a 1 GiB KV cache | [memory](../../evidence/edai2/llm_ab/local_real_models_20261106T192621Z/container_memory.txt) |

The exact 900/100 shows the gateway's split is deterministic in practice, not a random draw. Small samples still vary: 37/3 and 34/6 in two runs of 40, both inside tolerance.

These runs prove compatibility on the **CPU** build of vLLM. The CUDA build, the L4 driver and GPU memory are unproven until the GPU stages run. Local rate limits were raised for these tests. Latency here is laptop latency and is not a result.

## Experiments

EXP-V3A is sequential on one L4; the rendered arms differ on one line, the model path. EXP-V3B is the concurrent CPU and GPU run. The driver is [`llm_ab.sh`](../../../scripts/experiments/llm_ab.sh).

## GPU node

L4 is preferred: it supports BF16, the type the CPU stack uses. T4 lacks BF16, so it would need fp16 and other attention backends, which adds a difference to explain.

| Option | Price per hour | 3 h | 6 h |
|---|---:|---:|---:|
| g2-standard-4 + L4, Spot | $0.5233 | about $1.63 | about $3.26 |
| g2-standard-4 + L4, on demand | $0.8720 | about $2.70 | about $5.40 |

Totals include disk and IP. Spot is cheaper but can be reclaimed mid-run; a reclaimed run counts as failed and is not retried automatically. EXP-V3B also needs one CPU node for the CPU arm, at about $0.18/h.

Estimated memory is about 5.2 GiB for 0.6B and about 8 GiB for 1.7B, which fits the L4's 24 GB. These are calculations, not measurements.

## GPU requirements

| Item | State |
|---|---|
| L4 in `asia-southeast1-b` | Confirmed |
| `g2-standard-4` | 4 vCPU, 16 GiB; about 3 CPU and 11.6 GiB after DaemonSets |
| `GPUS_ALL_REGIONS` | **0, a hard blocker** |
| Image | `vllm/vllm-openai:v0.32.0` by digest, both models baked in; not yet built or scanned |
| CUDA | Image needs CUDA 13. The GKE driver is checked on the node before any run |
| Isolation | GPU taint and an L4 node selector; V3A uses its own namespace |
| Spot | A reclaimed run counts as failed and is not retried |
| Metrics | `ops/experiments/gpu_sampler.py` feeds GPU memory and utilization to the comparator |

Stages and approvals are in package P10 of the [approval packages](../../runbooks/edai2/C_C_APPROVAL_PACKAGES.md).

**Cleanup:** delete the pool and read back zero GPU nodes. A leftover L4 node costs about $12.50 a day on Spot.
