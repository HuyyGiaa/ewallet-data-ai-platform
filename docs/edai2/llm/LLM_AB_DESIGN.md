# LLM A/B design and GPU plan

This page plans two LLM comparisons that need a GPU. Neither has run, and neither can start until the GPU quota changes.

## Blocker

| Quota | Used / limit |
|---|---|
| `GPUS_ALL_REGIONS` | 0 / **0** |
| `NVIDIA_L4_GPUS` (asia-southeast1) | 0 / 1 |
| `PREEMPTIBLE_NVIDIA_L4_GPUS` (asia-southeast1) | 0 / 1 |
| `NVIDIA_T4_GPUS` (asia-southeast1) | 0 / 1 |
| `CPUS_ALL_REGIONS` | 16 / 32 |

The global GPU limit of 0 overrides the regional limits. Read on 2026-11-06. The billing account's Free Trial status is unverified, and a trial account cannot usually raise GPU quota.

**Owner decision:** requesting `GPUS_ALL_REGIONS = 1` is approved in principle, with L4 Spot preferred, a 3-hour budget target and a 6-hour session maximum. The owner handles the quota request. Until it's granted, no GPU resource exists or may be created.

## Two experiments

| | EXP-V3A | EXP-V3B |
|---|---|---|
| Question | What does the larger model cost on the same hardware? | Can one gateway run two arms live, with attribution? |
| Arms | Qwen3-0.6B, then Qwen3-1.7B | Qwen3-0.6B on CPU, Qwen3-1.7B on L4 |
| Timing | Sequential, one L4 | Concurrent, weighted |
| Fair? | Yes, only the model differs | **No, model and hardware differ together** |

**EXP-V3B has a built-in confounder.** It shows that routing and attribution work, and it may show which deployed arm serves users better. It cannot say why one arm is better, and it is not a serving optimization or an llm-d routing experiment.

## Routing

agentgateway v1.7.0 supports weighted routing through `virtualModels.routing.weighted`. A local test with the pinned image and a 90/10 split gave 269 and 31 of 300 requests, attributed by the `x-ewallet-backend` header.

Production BYO agents read `EWALLET_LLM_MODEL` and `EWALLET_LLM_BASE_URL` and do not use `ModelConfig`. A change to `ModelConfig` alone would not move their traffic, so the split must live in the gateway.

## GPU node

L4 is preferred: it supports BF16, the type the CPU stack uses. T4 lacks BF16, so it would need fp16 and other attention backends, which adds a difference to explain.

| Option | Price per hour | 3 h | 6 h |
|---|---:|---:|---:|
| g2-standard-4 + L4, Spot | $0.5233 | about $1.63 | about $3.26 |
| g2-standard-4 + L4, on demand | $0.8720 | about $2.70 | about $5.40 |

Totals include disk and IP. Spot is cheaper but can be reclaimed mid-run; a reclaimed run counts as failed and is not retried automatically. EXP-V3B also needs one CPU node for the CPU arm, at about $0.18/h.

Estimated memory is about 5.2 GiB for 0.6B and about 8 GiB for 1.7B, which fits the L4's 24 GB. These are calculations, not measurements.

## Before a run

1. The quota request is granted.
2. Build a GPU vLLM image with both models baked in, which is a separate approved scope. Pin its digest.
3. Add a pool with a `nvidia.com/gpu` taint, zero nodes at rest, and the same session rules as `vllm_bench.sh`: time limit, cleanup, and a production comparison before and after.
4. Add a GPU sampler. `gpu_memory_mib_peak` and `gpu_utilization_mean` in `configs/experiments/exp-v3a-model-l4.json` have no producer yet, and they will show as `n/a` until one exists.
5. Add quality checks for the workload. `tool_calls_valid` counts valid tool calls only, which is thin evidence that one model is better.

**Cleanup:** delete the pool and read back zero GPU nodes. A leftover L4 node costs about $12.50 a day on Spot.
