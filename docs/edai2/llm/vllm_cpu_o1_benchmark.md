# vLLM CPU serving optimization (O1): benchmark report

Status: **closed and frozen** (2026-11-01). The runtime benchmark is done. This
report records it but does not re-run or change it. Configuration **C1** goes
forward to G2 by **explicit owner review**. The original automated evaluator
did not select it (see [§8](#8-evaluator-errata)).

| Item | Value |
|---|---|
| Branch | `codex/llm-o1-vllm-cpu-optimization` (on top of G1 `dedea5e`) |
| Runtime commit (attempt 4) | `a194189d142bb0dfe98ea8882ce19f0b6f037f97` |
| Report generator commit | `84c34487f9efdfbe67262db028c96b77fea799e1` |
| Authoritative evidence | [`o1_20261101T052939Z`](../../evidence/llm_agent/o1_20261101T052939Z/) |
| Evaluator output (preserved) | `selected: NO_GAIN`, `baseline_valid: false` |
| Owner decision | C1 (`VLLM_CPU_OMP_THREADS_BIND=0-1`) selected for G2 |

## 1. Overview

O1 asked whether the G1 CPU deployment of vLLM on GKE could serve faster on
the same resources. G1 is the baseline: Qwen3-0.6B on vLLM 0.32.0 CPU, behind
agentgateway, running next to MLOps. Each candidate changed exactly one serving
parameter. Every configuration ran against the same fixed workload, on the same
two temporary nodes, with the same resources and replica count. An MLOps guard
watched the whole run.

Four attempts were run. Attempts 1–3 stopped early because of bugs in the
runner or the benchmark environment. None of those bugs were in the serving
configurations. Attempt 4 finished all four configurations (144 measured
requests, no HTTP 429). It then ran the G1 gates, cleaned up, and handed the
cluster back to its two MLOps nodes.

Headline (attempt 4, concurrency 4, versus baseline B0'):

| | Output tokens/s | TTFT p50 | E2E p95 | Errors |
|---|---|---|---|---|
| **C1** thread bind `0-1` | **+51.4 %** | **−37.4 %** | **−34.7 %** | 0 / 36 |
| C2 batched tokens 512 | −0.5 % | +23.8 % | +0.2 % | 1 / 36 |
| C3 prefix caching off | −100 % | n/a | n/a | 22 / 36 |

## 2. Research questions

1. **RQ1 (C1).** Does binding the vLLM CPU worker's OpenMP threads to both
   requested cores raise throughput and lower latency? In G1, `auto` bound
   inference to a single core
   ([O1 runbook](../../llm_agent/o1_runbook.md#experiments-same-resources-23-cpu-610-gib-kv-2-gib-1-active-backend)).
2. **RQ2 (C2).** Does a smaller prefill chunk (`--max-num-batched-tokens 512`)
   improve TTFT under mixed short/long concurrent load?
3. **RQ3 (C3, ablation).** How much does prefix caching contribute on CPU?
4. **RQ4 (coexistence).** Can any configuration run without degrading the
   protected MLOps workloads (`fraud-serving/fraud-detector`, 24 pods)?

## 3. Experimental environment

| Component | Value |
|---|---|
| Cluster | GKE `ewallet-dev`, project `ewallet-data-ai-platform-hg26`, zone `asia-southeast1-b`, 1.36.4-gke.2046003 |
| Node pool | `cpu-pool`, e2-standard-4 (allocatable 3.92 CPU / ~12.96 GiB) |
| O1 nodes | 2 temporary nodes labelled `ewallet.io/o1-llm=true` (attempt 4: `…-4z9k`, `…-9gpp`); MLOps stayed on `…-08s1`, `…-mb8v` |
| Mesh / policy | Istio ambient 1.30.7, Calico NetworkPolicy, NodeLocal DNSCache |
| vLLM | `vllm-openai-cpu:v0.32.0-x86_64@sha256:827ef381…0701`, model baked into the image `vllm-cpu-model@sha256:f695b23e…d07c` |
| Model | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca`, bfloat16, served as `ewallet-fraud-llm-v1` |
| Gateway | agentgateway v1.7.0 `@sha256:60216aaf…c45b`, client API key auth, failover primary → fallback |
| Per-pod resources | requests 2 CPU / 6 GiB, limits 3 CPU / 10 GiB, KV cache 2 GiB, `OMP_NUM_THREADS=2`, `max-model-len 4096` |
| Scheduling | primary `max-num-seqs 4` / `max-num-queued-reqs 8`; fallback 2 / 4 |
| Parsers | `--tool-call-parser hermes`, `--reasoning-parser qwen3`, `enable_thinking=false` |
| Gateway budget (benchmark only) | `o1/benchmark-gateway.yaml`: token rate 400 000/h (production: 24 000/h); request rate 12/s unchanged |

The exact image digests are in
[`image-digests.json`](../../evidence/llm_agent/o1_20261101T052939Z/image-digests.json).
The node budget check is in
[`capacity.json`](../../evidence/llm_agent/o1_20261101T052939Z/capacity.json)
(verdict `FITS_SAFE_BUDGET`).

## 4. Methodology

- **Runner.** `scripts/llm_agent/o1_experiment.sh` ran the steps in this order:
  1. capacity check, then scale up by two labelled nodes;
  2. G1 prerequisites (namespace, Secrets, Istio);
  3. for each configuration in turn: `helm upgrade`, then rollout, then
     benchmark;
  4. final redeploy of the selected config. With NO_GAIN this was B0'.
  5. G1 gates;
  6. cleanup: `helm uninstall`, then `delete-instances` on exactly the two
     labelled nodes.
- **Client.** `scripts/llm_agent/o1_benchmark.py` sent every request through
  the agentgateway port-forward, which is the production path. It recorded
  TTFT, ITL, TPOT, E2E and token counts per request. It also read vLLM
  Prometheus counters (prefix-cache hits and queries, queue time, preemptions)
  before and after each phase.
- **Workload `o1-workload-v1`** (sha256 `c4816ca1…d7695f`), identical for every
  configuration:
  - **Cold-prefix phase:** 3 requests sharing a new ~800-token prefix.
  - **Warm-up:** 2 requests, not measured.
  - **Measured phase:** 12 requests at each concurrency level 1, 2 and 4, so
    36 per configuration:
    - `short_chat` ×2: ~20 input tokens;
    - `long_chat` ×2: ~1 617 input / 128 output;
    - `prefix_repeat` ×3: ~584 tokens, shared prefix;
    - `prefix_unique` ×2: ~596 tokens;
    - `tool_call` ×2: streamed, with a real tool schema;
    - `nonstream_chat` ×1.
- **Guard.** `ops/llm_agent/o1_guard.py` sampled MLOps node and pod CPU and
  memory, vLLM restarts and cgroup throttling for every configuration window.
  It would have stopped the run on any MLOps regression. It never fired.
- **Selection rules** (`o1_compare.py`, recorded in `comparison.json` →
  `rules`). A candidate qualifies when all of the following hold:
  - it shows a gain: output tokens/s up at least 10 %, **or** TTFT p50 down at
    least 10 % with at most 5 % throughput loss;
  - E2E p95 rises by at most 10 % at every level;
  - it has 0 errors and 0 timeouts;
  - tool calls are not fewer;
  - there is no MLOps regression.

## 5. Configurations

Each candidate is B0' plus exactly one overlay from
`infra/helm/llm-agent-platform/o1/`. Every run also used `placement.yaml`
(nodeSelector for the temporary nodes) and `benchmark-gateway.yaml` (token
budget).

| Config | Overlay | Single change |
|---|---|---|
| **B0'** `b0-prime` | `o1/b0-prime.yaml` (empty) | none: G1 as it ran (`VLLM_CPU_OMP_THREADS_BIND=auto`, prefix caching on) |
| **C1** `c1-thread-bind` | `o1/c1-thread-bind.yaml` | `vllm.cpuThreadsBind: "0-1"` → `VLLM_CPU_OMP_THREADS_BIND=0-1` |
| **C2** `c2-batched-tokens` | `o1/c2-batched-tokens.yaml` | `vllm.maxNumBatchedTokens: 512` → `--max-num-batched-tokens 512` |
| **C3** `c3-no-prefix-cache` | `o1/c3-no-prefix-cache.yaml` | `prefixCaching: false` on primary and fallback → `--no-enable-prefix-caching` |

## 6. Results

All numbers come from attempt 4
([`comparison.md`](../../evidence/llm_agent/o1_20261101T052939Z/o1/comparison.md),
raw rows in `o1/<config>/raw_requests.jsonl`). Tokens/s is the total across the
level. "Per CPU" divides by the 2 requested CPUs.

### 6.1 Matched benchmark

| Config | Lvl | TTFT p50/p95 (ms) | ITL p50/p95 (ms) | TPOT p50 (ms) | E2E p50/p95 (ms) | req/s | out tok/s | tok/s/CPU | prefix hit | errors | vLLM CPU mean/peak (m) | RAM peak (MiB) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B0' | 1 | 7810/61393 | 205/294 | 204 | 13282/74456 | 0.023 | 1.10 | 0.55 | 0.43 | 8 % | 1238/1768 | 6197 |
| B0' | 2 | 8886/11563 | 389/511 | 463 | 34050/69608 | 0.058 | 3.44 | 1.72 | 0.89 | 0 % | 1660/1806 | 6197 |
| B0' | 4 | 12870/23424 | 699/822 | 817 | 59184/151065 | 0.061 | 3.62 | 1.81 | 0.89 | 0 % | 1663/1799 | 6196 |
| **C1** | 1 | 4918/60265 | 154/251 | 146 | 11161/74732 | 0.037 | 2.21 | 1.10 | 0.43 | 0 % | 2312/2696 | 6210 |
| **C1** | 2 | 5261/7495 | 274/352 | 318 | 22561/46602 | 0.087 | 5.18 | 2.59 | 0.89 | 0 % | 2676/2764 | 6206 |
| **C1** | 4 | 8058/13183 | 511/614 | 569 | 39800/98717 | 0.092 | 5.48 | 2.74 | 0.89 | 0 % | 2654/2834 | 6207 |
| C2 | 1 | 7685/61313 | 211/307 | 212 | 13620/74906 | 0.023 | 1.13 | 0.57 | 0.43 | 8 % | 1295/2007 | 6191 |
| C2 | 2 | 9449/17985 | 395/511 | 441 | 34818/69350 | 0.056 | 3.31 | 1.65 | 0.89 | 0 % | 1646/2005 | 6189 |
| C2 | 4 | 15931/23349 | 709/815 | 800 | 60860/151337 | 0.061 | 3.60 | 1.80 | 0.89 | 0 % | 1645/1891 | 6189 |
| C3 | 1 | 40994/96694 | 209/307 | 215 | 46051/110719 | 0.014 | 0.54 | 0.27 | – | 17 % | 1125/1670 | 6203 |
| C3 | 2 | 57582/104422 | 337/429 | 235 | 61272/110531 | 0.015 | 0.15 | 0.08 | – | 67 % | 1047/1511 | 6197 |
| C3 | 4 | – | – | – | – | 0.025 | 0.00 | 0.00 | – | 100 % | 1047/1354 | 6194 |

### 6.2 Cold vs warm prefix (TTFT, ms)

| Config | 1st (cold) | 2nd | 3rd |
|---|---|---|---|
| B0' | 84 758 | 4 476 | 4 244 |
| C1 | 43 839 | 2 647 | 2 670 |
| C2 | 85 092 | 4 447 | 4 288 |
| C3 | 87 788 | 88 258 | 87 460 |

### 6.3 Failed requests

| Config | Failed / 36 | Detail |
|---|---|---|
| B0' | 1 | level 1 `long-1`: HTTP 200, 0 output tokens, E2E 161 196 ms. The same request succeeded at levels 2 and 4 |
| C1 | 0 | – |
| C2 | 1 | level 1 `long-1`: HTTP 200, 0 output tokens, E2E 161 143 ms (same pattern as B0') |
| C3 | 22 | all levels and every category: 16 long or prefix requests, 3 short, 2 tool, 1 non-stream. Each ended at 160–173 s with 0 output tokens (21 with HTTP 200, 1 with no status) |

All of these failures end at 160 s or later. That matches the 160 s backend
timeout in the gateway's CPU budget, which closes the stream. No request
returned HTTP 429.

### 6.4 Resource isolation and MLOps coexistence

| Config | Guard samples | MLOps regressions | vLLM restarts | CPU throttled periods | MLOps node CPU / mem peak |
|---|---|---|---|---|---|
| B0' | 54 | 0 | 0 | 0 / 10 746 | 10 % / 44 % |
| C1 | 34 | 0 | 0 | 304 / 6 709 (4.5 %) | 10 % / 44 % |
| C2 | 54 | 0 | 0 | 0 / 10 726 | 10 % / 44 % |
| C3 | 123 | 0 | 0 | 0 / 24 158 | 10 % / 44 % |

MLOps was healthy before, during and after the run: 24 pods, with
`fraud-serving/fraud-detector` Ready
([before](../../evidence/llm_agent/o1_20261101T052939Z/mlops-health-before.json),
[during](../../evidence/llm_agent/o1_20261101T052939Z/mlops-health-during.json),
[after](../../evidence/llm_agent/o1_20261101T052939Z/mlops-health-after.json)).

### 6.5 G1 gates (run on the final redeploy, i.e. **B0'**, not C1)

| Gate | Result | Evidence |
|---|---|---|
| NetworkPolicy, 6 probes | 6/6 denied as expected | [`ops/netpol-verify.tsv`](../../evidence/llm_agent/o1_20261101T052939Z/ops/netpol-verify.tsv) |
| Client auth (wrong / missing / backend key) | 401 / 401 / 401, on both the direct and the gateway path | [`ops/smoke-gateway-auth.txt`](../../evidence/llm_agent/o1_20261101T052939Z/ops/smoke-gateway-auth.txt), [`ops/smoke-direct-auth.txt`](../../evidence/llm_agent/o1_20261101T052939Z/ops/smoke-direct-auth.txt) |
| Smoke: stream, non-stream, tool call | PASS on direct and gateway | `ops/smoke-*-stream.json`, `ops/smoke-*-non-stream.json` |
| Failover | attempt 1: 503 from primary; attempt 2: 200 from fallback. Fallback `vllm:request_success_total` went 1 → 6 | [`ops/failover/eviction.txt`](../../evidence/llm_agent/o1_20261101T052939Z/ops/failover/eviction.txt), [`fallback-metric.txt`](../../evidence/llm_agent/o1_20261101T052939Z/ops/failover/fallback-metric.txt) |
| Cleanup | release uninstalled; pool back to the 2 MLOps nodes | [`cleanup.txt`](../../evidence/llm_agent/o1_20261101T052939Z/cleanup.txt) |

## 7. Analysis

**C1: thread binding (RQ1). This is a large, consistent gain.**

| C1 vs B0' | Level 1 | Level 2 | Level 4 |
|---|---|---|---|
| Output tokens/s | +101 % | +50.6 % | +51.4 % |
| TTFT p50 | −37.0 % | −40.8 % | −37.4 % |
| E2E p95 | +0.4 % | −33.1 % | −34.7 % |
| ITL p50 | −24.9 % | −29.6 % | −26.9 % |

- **Cause.** Mean vLLM CPU rose from ~1.66 to ~2.65 cores at levels 2 and 4.
  This matches the hypothesis: under `auto`, the worker computed on one core
  and the second requested core sat mostly idle.
- **Cold prefill.** An ~800-token cold prefill halved, from 84.8 s to 43.8 s.
- **Errors.** C1 also had no level-1 `long-1` failure. Its faster prefill kept
  that request under the gateway's 160 s budget.
- **Level 1 is the weakest comparison.** Because of the B0' failure, B0' has
  11 successful rows against C1's 12. The conclusion rests on levels 2 and 4,
  where both have 0 errors and C1 still wins clearly.
- **Cost of the gain.**
  - C1 was throttled in 4.5 % of CFS periods; the others in 0 %. It now runs
    against its 3-CPU limit.
  - Peak usage reached 2.83 cores, above the 2-CPU request.
  - Memory did not change: about 6.2 GiB peak.

**C2: smaller prefill chunks (RQ2). No benefit.** Throughput did not change.
TTFT p50 was about the same at level 1 (−1.6 %) and worse at levels 2 and 4
(+6.3 %, +23.8 %). The B0' `long-1` failure repeated. On a
worker that effectively uses one core, splitting prefill into 512-token chunks
adds scheduling steps but frees no compute for short requests.

**C3: prefix caching off (RQ3). Strongly harmful.**
- Every request paid full prefill: about 87 s for ~800 tokens, against ~4.4 s
  warm with the cache.
- Errors were 17 % at level 1, 67 % at level 2 and 100 % at level 4, all
  against the gateway timeout.
- **Conclusion:** prefix caching is essential on CPU and must stay enabled.

**Coexistence (RQ4).** No configuration affected MLOps. The O1 pods ran only on
the labelled temporary nodes. MLOps nodes peaked at 10 % CPU and 44 % memory
throughout, and the guard recorded 0 regressions.

## 8. Evaluator errata

**E1: the baseline check vetoes every candidate (affects the outcome).**

`scripts/llm_agent/o1_compare.py`, lines 108–114, at `a194189` / `84c3448`:

```python
base_ok = evaluate(summaries[BASE], summaries[BASE], windows[BASE])
base_ok["reasons"] = [r for r in base_ok["reasons"] if not r.startswith("no defensible gain")]
...
"selected": qualifying[0] if qualifying and not base_ok["reasons"] else "NO_GAIN",
```

- **What the code does.** The baseline is run through the candidate
  qualification rules against itself. Any reason that remains makes
  `baseline_valid: false`, and that alone forces `selected: NO_GAIN`, whatever
  the candidates scored.
- **What happened in attempt 4.** One B0' request out of 36 failed: level 1
  `long-1`, a gateway-side timeout (§6.3). That produced
  `baseline_issues: ["level 1: errors 8%, timeouts 0%"]`. The same
  `comparison.json` records C1 as `qualifies: true, reasons: []`.
- **Why this is a defect.** The check was meant to catch a broken baseline,
  such as a failed deploy or a systematic error rate. It turned one observed
  baseline request failure into a global disqualification. That failure is
  itself a measured weakness of the baseline, and C1 removes it.

Disposition:
- The evaluator source, the thresholds, `comparison.json`, `selected.txt` and
  `MANIFEST.sha256` are **not modified**.
- No corrected evaluator output has been produced.
- For any future O-series evaluator, a suggested fix is to distinguish an
  invalid baseline from baseline request errors, and to emit
  `REVIEW_REQUIRED` instead of `NO_GAIN` when candidates qualify. This needs
  its own change and review, and it is out of scope here.

**E2: the `comparison.md` table has a malformed delimiter row (cosmetic).**
The header has 14 columns and the delimiter row has 15. Some Markdown
renderers will therefore not draw it as a table. The data rows are correct.
The file is preserved as generated, and §6.1 reproduces the values.

**E3: the attempt 1 cleanup readback came too early (evidence quality).**
Attempt 1's `cleanup.txt` lists 4 nodes. The readback ran before node deletion
had finished; this was fixed in `a08f64b`. Attempt 2's `capacity.json`, taken
at 03:47Z, shows only the MLOps nodes plus attempt 2's own new nodes. So the
attempt 1 nodes (`…-8wnf`, `…-mfgh`) were gone by then.

## 9. Configuration selection

> The original automated selection reported NO_GAIN because its
> baseline-validation logic treated an observed baseline request failure as a
> global disqualification. The raw matched results indicate C1 satisfies the
> recorded candidate qualification criteria and provides the best measured
> performance. C1 is therefore selected by explicit owner review for G2, while
> the original evaluator output is preserved for traceability.

| Config | Evaluator `qualifies` | Decision |
|---|---|---|
| C1 `c1-thread-bind` | true (no reasons) | **Selected for G2** (owner review, 2026-11-01) |
| C2 `c2-batched-tokens` | false: level 1 errors 8 %; no gain (−0.5 % tok/s, TTFT +24 %) | rejected |
| C3 `c3-no-prefix-cache` | false: errors at every level; tail +49 % / +59 %; tool calls lost | rejected; prefix caching stays on |

## 10. Limitations

1. **One run per configuration, 12 requests per level.** There are no
   repetitions and no confidence intervals. A p95 over 12 samples is close to
   the maximum. The C1 effect (+50 % throughput at two levels) is far larger
   than the run-to-run noise seen between B0' and C2, which differ by only one
   parameter that had no effect. Still, it is a single observation.
2. **Configurations ran in sequence** (B0' → C1 → C2 → C3) on the same nodes,
   not interleaved. Time-dependent drift cannot be excluded. C2, which ran
   after C1, reproduced B0' almost exactly, which argues against large drift.
3. **The G1 gates were not run on C1.** The final redeploy was B0', because
   the evaluator returned NO_GAIN. NetworkPolicy, auth, smoke and failover are
   verified only for B0'.
4. **CPU pinning may contend with other pods.** `0-1` pins to fixed CPU IDs
   under the node's default (non-static) CPU manager. On a node shared with
   other workloads, cores 0–1 may contend with them. O1 nodes carried only the
   O1 release and DaemonSets.
5. **C1 runs at its CPU limit.** It was throttled in 4.5 % of periods and
   peaked at 2.83 cores, above its 2-CPU request.
6. **The workload is synthetic**: one small model (Qwen3-0.6B) and one
   workload. Tool-call validity is checked structurally only, with no
   semantic grading.
7. **The benchmark used a raised gateway token budget** (400 000/h,
   owner-approved for the O1 session only). Production stays at 24 000/h,
   under which one benchmark pass alone used about 20 000 tokens.
8. **Attempts 1–3 cannot be compared with attempt 4.** They ran different
   runner commits and stopped early. They are kept for traceability only.

## 11. Reproducibility & evidence

**Attempts.** All four are preserved in place and unmodified.
- **Tree hash** = `sha256` over the sorted `sha256sum` list of every file in the
  directory.
- **Total cloud time** across attempts was about 5.9 h of two extra
  e2-standard-4 nodes, roughly US $2 (estimate).

| Attempt | Directory | Runner commit | Window (UTC) | Exit | Outcome | Files | Tree hash |
|---|---|---|---|---|---|---|---|
| 1 | [`o1_20261101T025115Z`](../../evidence/llm_agent/o1_20261101T025115Z/) | `7c40617` | 02:51–03:46 | 1 | Port-forward leaked through a function wrapper (`$!` was a subshell). C1 requests hit deleted B0' pods | 54 | `435030ad…4535a` |
| 2 | [`o1_20261101T034723Z`](../../evidence/llm_agent/o1_20261101T034723Z/) | `a08f64b` | 03:47–04:30 | 143 | `wait` on a killed port-forward returned 143 under `set -e` | 43 | `ce6ad5d5…58bbc` |
| 3 | [`o1_20261101T043036Z`](../../evidence/llm_agent/o1_20261101T043036Z/) | `2d1eb68` | 04:30–05:29 | 130 | Stopped by the operator: the production token budget (24 000/h) was exhausted by B0', and every C1 request got 429 | 60 | `8efa6355…4a851` |
| **4** | [`o1_20261101T052939Z`](../../evidence/llm_agent/o1_20261101T052939Z/) | `a194189` | 05:29–08:46 | 0 | **Complete.** 144 requests, 0 × 429, evaluator `NO_GAIN` (§8) | 127 | `9597bfc0…10c0e` |

**Attempt 4 integrity.**
- [`MANIFEST.sha256`](../../evidence/llm_agent/o1_20261101T052939Z/MANIFEST.sha256)
  covers 126 files and verifies OK. The manifest's own sha256 is `926edb30…c694`.
- The sha256 of `o1/comparison.json` is
  `d1d27f4444f79fe186a762d4f4504ee68c97a1a69db21cf33f4db6eb57606c42`.

To check:

```bash
(cd docs/evidence/llm_agent/o1_20261101T052939Z && sha256sum -c --quiet MANIFEST.sha256)
```

**Attempt 4 evidence map:**

| Path (under `o1_20261101T052939Z/`) | Contents |
|---|---|
| [`O1_REPORT.md`](../../evidence/llm_agent/o1_20261101T052939Z/O1_REPORT.md) | generated by `o1_report.py` |
| [`o1/comparison.json`](../../evidence/llm_agent/o1_20261101T052939Z/o1/comparison.json), [`selected.txt`](../../evidence/llm_agent/o1_20261101T052939Z/selected.txt) | original evaluator output (`NO_GAIN`) |
| `o1/<config>/raw_requests.jsonl`, `summary.json`, `window.json`, `workload.json` | per-request rows, per-level summaries, guard window, workload fingerprint |
| `o1/<config>/deploy/` | `helm upgrade` output, history, rollout, workloads |
| `state/resources.jsonl` | guard samples |
| `ops/` | G1 gates on the final B0' redeploy |
| `o1.log`, `run.log`, `ops/operator.log` | runner logs |

**Security screening before commit.**
- A Trivy 0.75.0 secret scan and pattern searches (Bearer tokens, private
  keys, cloud access keys, GitHub tokens, kubeconfig data) found nothing.
- `ops/secret.txt` records only that the Secrets existed and were kept. It
  contains no values.
- No file in the evidence is larger than 1 MB.

**Reproduce:**
- **Run:** see the [O1 runbook](../../llm_agent/o1_runbook.md). Check out
  `a194189`, then run `scripts/llm_agent/o1_experiment.sh <evidence-dir>`.
  This needs `O1_APPROVED=yes` and owner approval for the two temporary nodes.
- **Re-render the report offline:**
  `python3 scripts/llm_agent/o1_report.py docs/evidence/llm_agent/o1_20261101T052939Z`.

**Navigation.**
- `docs/README.md` is a shared path that other workstreams also edit, so this
  change does not touch it.
- Entry to add when the index is next updated:
  `- [vLLM CPU O1 benchmark](edai2/llm/vllm_cpu_o1_benchmark.md) — O1 serving optimization results, evaluator errata, C1 handoff to G2`

## 12. G2 handoff

**Configuration handed over: C1.** This is G1 (B0') plus a single value:

```yaml
vllm:
  cpuThreadsBind: "0-1"   # VLLM_CPU_OMP_THREADS_BIND=0-1; G1 default is "auto"
```

Source: `infra/helm/llm-agent-platform/o1/c1-thread-bind.yaml`. The chart knob
is in `templates/vllm.yaml`. Every other value is identical to G1.

**Do not carry these into G2 unless they are approved again:**
- `o1/benchmark-gateway.yaml`: the 400 000/h token budget was for the
  benchmark only. G2 uses the production 24 000/h.
- `o1/placement.yaml`: it targets the temporary O1 nodes, which no longer
  exist.

**G2 entry conditions (none are done yet):**
1. Run the full G1 gate set on **C1**: NetworkPolicy, client auth 401s, direct
   and gateway smoke (stream, non-stream, tool call), and failover with
   eviction. O1 verified these only on B0'.
2. Decide where C1 is placed. On nodes shared with other workloads, check
   contention on pinned cores 0–1, or use dedicated or labelled nodes.
3. Watch CFS throttling, which was 4.5 % in O1. Decide whether to keep the
   3-CPU limit or raise it, within the MLOps safe budget.
4. Confirm MLOps coexistence on the target placement. The guard thresholds
   are unchanged.
5. Keep prefix caching on (C3 result).
6. Integrate into canonical and reproduce after merge. Neither is done.

**Status at O1 closure:**

| Flag | Value |
|---|---|
| `O1_RUNTIME_COMPLETED` | YES |
| `O1_BENCHMARK_FROZEN` | YES |
| `O1_EVIDENCE_PRESERVED` | YES: all four attempts committed in place, attempt 4 manifest verified |
| `O1_DOCUMENTATION_COMPLETED` | YES (this report) |
| `O1_EVALUATOR_SOURCE_MODIFIED` | NO |
| `G2_RUNTIME_STARTED` | NO |
| `G2_SECURITY_VERIFIED` | NO |
| `INTEGRATED_CANONICAL` | NO |
| `POST_MERGE_REPRODUCED` | NO |
